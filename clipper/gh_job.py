"""Run clip jobs inside GitHub Actions and publish the results as a GitHub release.

A job is a small JSON file committed to clipper/jobs/requests/<id>.json:

    {"url": "https://www.youtube.com/watch?v=...", "count": 5, "captions": true}

The workflow calls:

    python -m clipper.gh_job start <request.json>...   # stdlib only: runs before deps install
    python -m clipper.gh_job run   <request.json>...   # does the work, uploads clips
    python -m clipper.gh_job fail  <request.json>...   # marks jobs failed if a step crashed

Each job becomes the prerelease `clips-<id>`. Its body holds a readable
summary plus the machine-readable state between <!-- clipper: ... --> markers,
which the web page polls. The clips and posters are the release's assets.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "")
RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}"
           f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
JOB_ID = re.compile(r"^[a-z0-9][a-z0-9-]{5,40}$")
STAGE_LABELS = {
    "queued": "Waiting for a runner", "setup": "Setting up", "download": "Downloading video",
    "probe": "Reading video", "audio": "Extracting audio", "transcribe": "Transcribing",
    "pick": "Finding & scoring the best moments", "render": "Rendering vertical clips",
    "upload": "Uploading clips", "done": "Done",
}


# ------------------------------------------------------------------ GitHub REST


def _call(method: str, url: str, body: dict | bytes | None = None,
          content_type: str = "application/json") -> dict | None:
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Content-Type": content_type} if data is not None else {}),
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"GitHub API {method} {url} -> {exc.code}: {exc.read()[:300]!r}") from exc
        except urllib.error.URLError:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return None


def _body(state: dict) -> str:
    """Readable markdown for people on github.com + JSON for the web page."""
    blob = json.dumps(state, ensure_ascii=False).replace("--", "\\u002d\\u002d")
    lines = [f"<!-- clipper:{blob} -->", ""]
    status = state.get("status")
    lines.append(f"**Source:** {state.get('url', '')}")
    if status == "working":
        lines += ["", f"⏳ **{STAGE_LABELS.get(state.get('stage'), 'Working')}…** "
                      f"Refresh this page in a minute. [Live log]({RUN_URL})"]
    elif status == "error":
        lines += ["", f"❌ **Failed:** {state.get('error', 'unknown error')}", "", f"[Log]({RUN_URL})"]
    elif status == "done":
        res = state.get("result", {})
        lines += ["", f"✅ **{len(res.get('clips', []))} clips**, scored by "
                      f"{'Claude' if res.get('picked_by') == 'claude' else 'the audio heuristic'}. "
                      "Download them from **Assets** below.", "",
                  "| # | Score | Hook | Flow | Value | Time | Title | File |",
                  "|---|---|---|---|---|---|---|---|"]
        for i, c in enumerate(res.get("clips", []), 1):
            t = f"{int(c['start'] // 60)}:{int(c['start'] % 60):02d}"
            title = str(c.get("title", "")).replace("|", "/")
            lines.append(f"| {i} | **{c['score']}** | {c['hook']} | {c['flow']} | {c['value']} "
                         f"| {t} | {title} | `{c['file']}` |")
    return "\n".join(lines)


def _release(job_id: str) -> dict | None:
    return _call("GET", f"{API}/repos/{REPO}/releases/tags/clips-{job_id}")


def _save(job_id: str, state: dict) -> dict:
    rel = _release(job_id)
    payload = {"name": f"Clips {job_id}", "body": _body(state), "prerelease": True}
    if rel:
        return _call("PATCH", f"{API}/repos/{REPO}/releases/{rel['id']}", payload) or rel
    return _call("POST", f"{API}/repos/{REPO}/releases",
                 {**payload, "tag_name": f"clips-{job_id}", "target_commitish": BRANCH or "HEAD"})


def _upload(rel: dict, path: Path, content_type: str) -> None:
    for a in rel.get("assets", []) or []:
        if a["name"] == path.name:  # re-run: replace
            _call("DELETE", f"{API}/repos/{REPO}/releases/assets/{a['id']}")
    _call("POST", f"{UPLOADS}/repos/{REPO}/releases/{rel['id']}/assets?name={path.name}",
          path.read_bytes(), content_type)


# ------------------------------------------------------------------------ jobs


def _load(req_path: str) -> tuple[str, dict]:
    job_id = Path(req_path).stem.lower()
    if not JOB_ID.match(job_id):
        raise ValueError(f"bad job id {job_id!r}")
    req = json.loads(Path(req_path).read_text(encoding="utf-8"))
    url = str(req.get("url", "")).strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("That doesn't look like a video link.")
    count = max(1, min(10, int(req.get("count", 5))))
    return job_id, {"url": url, "count": count, "captions": bool(req.get("captions", True))}


def cmd_start(paths: list[str]) -> None:
    for p in paths:
        try:
            job_id, req = _load(p)
        except Exception as exc:
            print(f"skip {p}: {exc}")
            continue
        _save(job_id, {"status": "working", "stage": "setup", "pct": 0, **req,
                       "run": RUN_URL, "updated": int(time.time())})
        print(f"started {job_id}")


def cmd_fail(paths: list[str]) -> None:
    for p in paths:
        try:
            job_id, req = _load(p)
        except Exception:
            continue
        rel = _release(job_id)
        if rel and '"status": "done"' in (rel.get("body") or ""):
            continue
        _save(job_id, {"status": "error", **req, "run": RUN_URL, "updated": int(time.time()),
                       "error": "The job crashed during setup. Open the log for details."})


def _poster(clip: Path, out: Path) -> None:
    from clipper.pipeline import ffmpeg_exe

    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", "1.5", "-i", str(clip), "-frames:v", "1",
                    "-vf", "scale=360:640", "-q:v", "4", str(out)], check=True)


def run_one(path: str) -> None:
    from clipper.pipeline import make_clips

    job_id, req = _load(path)
    state = {"status": "working", "stage": "download", "pct": 0, **req, "run": RUN_URL}
    last = {"t": 0.0, "stage": None}

    def progress(stage: str, pct: float) -> None:
        now = time.time()
        # Only talk to GitHub on stage changes or every ~20 s, to stay well under rate limits.
        if stage == last["stage"] and now - last["t"] < 20:
            return
        last.update(t=now, stage=stage)
        state.update(stage=stage, pct=round(pct, 2), updated=int(now))
        try:
            _save(job_id, state)
        except Exception as exc:  # progress is best-effort
            print(f"progress update failed: {exc}")

    work = Path(tempfile.mkdtemp(prefix=f"clip-{job_id}-"))
    try:
        result = make_clips(req["url"], work / "clips", work, progress,
                            captions=req["captions"], count=req["count"])
        progress("upload", 0.0)
        rel = _save(job_id, {**state, "stage": "upload", "updated": int(time.time())})
        for c in result["clips"]:
            clip = work / "clips" / c["file"]
            poster = clip.with_name(clip.stem + ".jpg")
            _poster(clip, poster)
            _upload(rel, clip, "video/mp4")
            _upload(rel, poster, "image/jpeg")
            c["poster"] = poster.name
        _save(job_id, {"status": "done", **req, "run": RUN_URL, "updated": int(time.time()),
                       "result": result})
        print(f"done {job_id}: {len(result['clips'])} clips")
    except Exception as exc:
        traceback.print_exc()
        _save(job_id, {"status": "error", **req, "run": RUN_URL, "updated": int(time.time()),
                       "error": str(exc)[:500]})
        raise


def cmd_run(paths: list[str]) -> None:
    failed = 0
    for p in paths:
        try:
            run_one(p)
        except Exception:
            failed += 1
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("start", "run", "fail"):
        sys.exit("usage: python -m clipper.gh_job {start|run|fail} <request.json>...")
    files = [f for f in sys.argv[2:] if f.strip()]
    {"start": cmd_start, "run": cmd_run, "fail": cmd_fail}[sys.argv[1]](files)
