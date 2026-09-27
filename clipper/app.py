"""Tiny local web UI for the clipper: upload a video, get ranked 30 s vertical clips.

    python -m clipper.app        # then open http://localhost:8000

Set CLIP_PASSWORD to put the whole site behind a login (required when it's
reachable from the internet — see DEPLOY.md).
"""

from __future__ import annotations

import hmac
import os
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

from .pipeline import make_clips

HERE = Path(__file__).parent
WORK = Path(os.environ.get("CLIP_WORK_DIR", HERE / "work"))
STAGES = ["probe", "audio", "transcribe", "pick", "render", "done"]
MAX_CLIPS = 10
PASSWORD = os.environ.get("CLIP_PASSWORD", "")
KEEP_HOURS = float(os.environ.get("CLIP_KEEP_HOURS", "24"))

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("CLIP_MAX_MB", "4096")) * 1024 * 1024
jobs: dict[str, dict] = {}
# One job at a time: whisper + encoding already saturate the CPU.
render_lock = threading.Lock()


@app.before_request
def require_login():
    """HTTP Basic auth on everything but the health check. Any username works."""
    if not PASSWORD or request.path == "/healthz":
        return None
    auth = request.authorization
    given = (auth.password or "") if auth else ""
    if hmac.compare_digest(given.encode(), PASSWORD.encode()):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Clipper"'})


def _cleanup() -> None:
    """Delete uploads and clips older than KEEP_HOURS so the disk doesn't fill up."""
    if KEEP_HOURS <= 0 or not WORK.exists():
        return
    cutoff = time.time() - KEEP_HOURS * 3600
    for folder in WORK.iterdir():
        job = jobs.get(folder.name)
        if job and job["state"] in ("queued", "running"):
            continue
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            jobs.pop(folder.name, None)


def _run(job_id: str, src: Path, captions: bool, count: int) -> None:
    job = jobs[job_id]

    def progress(stage: str, pct: float) -> None:
        job["stage"], job["stage_pct"] = stage, pct

    with render_lock:
        job["state"] = "running"
        try:
            job["result"] = make_clips(src, src.parent / "clips", src.parent, progress,
                                       captions, count)
            job["state"] = "done"
        except Exception as exc:
            traceback.print_exc()
            job["state"], job["error"] = "error", str(exc)


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return send_from_directory(HERE / "static", "index.html")


@app.post("/api/clip")
def create_clip():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="No video uploaded"), 400
    _cleanup()
    job_id = uuid.uuid4().hex[:12]
    folder = WORK / job_id
    folder.mkdir(parents=True)
    ext = Path(f.filename).suffix.lower() or ".mp4"
    src = folder / f"source{ext}"
    f.save(src)
    jobs[job_id] = {"state": "queued", "stage": "queued", "stage_pct": 0.0, "name": f.filename}
    captions = request.form.get("captions", "1") != "0"
    try:
        count = max(1, min(MAX_CLIPS, int(request.form.get("count", "5"))))
    except ValueError:
        count = 5
    threading.Thread(target=_run, args=(job_id, src, captions, count), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/clip/<job_id>")
def clip_status(job_id: str):
    job = jobs.get(job_id) or abort(404)
    return jsonify(job | {"stages": STAGES})


@app.get("/api/clip/<job_id>/video/<int:n>")
def clip_video(job_id: str, n: int):
    job = jobs.get(job_id) or abort(404)
    if job["state"] != "done":
        abort(409)
    if not 1 <= n <= len(job["result"]["clips"]):
        abort(404)
    name = f"{Path(job['name']).stem}_clip{n}.mp4"
    return send_file(WORK / job_id / "clips" / f"clip_{n}.mp4", mimetype="video/mp4",
                     as_attachment=request.args.get("download") == "1", download_name=name)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    if not PASSWORD:
        print("WARNING: CLIP_PASSWORD is not set — anyone who can reach this port can use it.")
    print(f"Clipper running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
