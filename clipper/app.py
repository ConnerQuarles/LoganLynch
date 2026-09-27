"""Tiny local web UI for the clipper: upload a video, get a 30 s vertical clip.

    python -m clipper.app        # then open http://localhost:8000
"""

from __future__ import annotations

import os
import threading
import traceback
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from .pipeline import make_clip

HERE = Path(__file__).parent
WORK = Path(os.environ.get("CLIP_WORK_DIR", HERE / "work"))
STAGES = ["probe", "audio", "transcribe", "pick", "reframe", "render", "done"]

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("CLIP_MAX_MB", "4096")) * 1024 * 1024
jobs: dict[str, dict] = {}
# One clip at a time: whisper + encoding already saturate the CPU.
render_lock = threading.Lock()


def _run(job_id: str, src: Path, captions: bool) -> None:
    job = jobs[job_id]

    def progress(stage: str, pct: float) -> None:
        job["stage"], job["stage_pct"] = stage, pct

    with render_lock:
        job["state"] = "running"
        try:
            job["result"] = make_clip(src, src.parent / "clip.mp4", src.parent, progress, captions)
            job["state"] = "done"
        except Exception as exc:
            traceback.print_exc()
            job["state"], job["error"] = "error", str(exc)


@app.get("/")
def index():
    return send_from_directory(HERE / "static", "index.html")


@app.post("/api/clip")
def create_clip():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="No video uploaded"), 400
    job_id = uuid.uuid4().hex[:12]
    folder = WORK / job_id
    folder.mkdir(parents=True)
    ext = Path(f.filename).suffix.lower() or ".mp4"
    src = folder / f"source{ext}"
    f.save(src)
    jobs[job_id] = {"state": "queued", "stage": "queued", "stage_pct": 0.0, "name": f.filename}
    captions = request.form.get("captions", "1") != "0"
    threading.Thread(target=_run, args=(job_id, src, captions), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/clip/<job_id>")
def clip_status(job_id: str):
    job = jobs.get(job_id) or abort(404)
    return jsonify(job | {"stages": STAGES})


@app.get("/api/clip/<job_id>/video")
def clip_video(job_id: str):
    job = jobs.get(job_id) or abort(404)
    if job["state"] != "done":
        abort(409)
    name = Path(job["name"]).stem + "_clip.mp4"
    return send_file(WORK / job_id / "clip.mp4", mimetype="video/mp4",
                     as_attachment=request.args.get("download") == "1", download_name=name)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    print(f"Clipper running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
