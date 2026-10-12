"""Clipper as a website on your own computer.

    python -m clipper.app        # opens http://localhost:8000 in your browser

Serves the Clipper page (clipper/browser/clipper.html, the same page as the
claude.ai version) and adds what a page can't do on its own:

  * pasted links: downloads YouTube/TikTok/Instagram/X/direct videos with yt-dlp
    over your own internet connection, then hands the file to the page
  * Claude scoring with your Anthropic API key (saved in ~/.clipper/config.json)
  * plain file downloads for finished clips

Speech-to-text still runs in the browser (Whisper); its ~130 MB of files are
built from npm on first start (clipper/assets.py).

It only answers requests addressed to this computer. To serve other devices,
set CLIP_PASSWORD (and CLIP_HOST=0.0.0.0); see DEPLOY.md.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

from . import assets
from .fetch import download, is_url

HERE = Path(__file__).parent
PAGE = HERE / "browser" / "clipper.html"
WHISPER = assets.OUT
WORK = Path(os.environ.get("CLIP_WORK_DIR", HERE / "work"))
CONFIG = Path(os.environ.get("CLIP_CONFIG", Path.home() / ".clipper" / "config.json"))
PASSWORD = os.environ.get("CLIP_PASSWORD", "")
KEEP_HOURS = float(os.environ.get("CLIP_KEEP_HOURS", "24"))
DEFAULT_MODEL = "claude-opus-5"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
# Windows' registry can map .js to text/plain, which breaks module scripts: be explicit.
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm",
         ".json": "application/json", ".txt": "text/plain; charset=utf-8"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024   # Claude requests carry a few JPEGs
fetches: dict[str, dict] = {}


# ---------------------------------------------------------------------------- guard


@app.before_request
def guard():
    if request.path == "/healthz":
        return None
    if PASSWORD:
        auth = request.authorization
        given = (auth.password or "") if auth else ""
        if hmac.compare_digest(given.encode(), PASSWORD.encode()):
            return None
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Clipper"'})
    # No password: answer only requests addressed to this computer. That keeps other
    # devices, and web pages using DNS tricks, from spending your Claude credits.
    host = request.host or ""
    host = host[1:host.find("]")] if host.startswith("[") else host.split(":")[0]
    if host.lower() not in LOOPBACK:
        return Response("Clipper only answers on localhost. Set CLIP_PASSWORD to serve other devices.", 403)
    if request.method == "POST" and not request.is_json:
        # JSON-only POSTs force a CORS preflight, which other sites can't pass.
        return jsonify(error="Expected JSON."), 415
    return None


# --------------------------------------------------------------------------- config


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        CONFIG.chmod(0o600)   # the API key lives here
    except OSError:
        pass


def api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or load_config().get("anthropic_api_key", "")


def workspace_id() -> str:
    return os.environ.get("ANTHROPIC_WORKSPACE_ID") or load_config().get("anthropic_workspace_id", "")


def model_name() -> str:
    return os.environ.get("CLIP_CLAUDE_MODEL") or load_config().get("model") or DEFAULT_MODEL


# ----------------------------------------------------------------------------- page

# Stands in for the claude.ai runtime the page was written against: "sample" calls
# Claude through this server, "downloads" saves files the normal way.
SHIM = """<script>
window.CLIPPER_LOCAL = true;
(() => {
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const b64 = blob => new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1]); r.onerror = bad; r.readAsDataURL(blob); });
  async function ask(body){
    let r; try { r = await post("/api/claude", body); } catch { throw { code: "upstream_error", message: "Clipper's server isn't running." }; }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw { code: d.code || "upstream_error", message: d.error || "Claude request failed." };
    return d;
  }
  const sample = async prompt => ({ text: (await ask({ prompt, raw: true })).text, truncated: false });
  sample.limits = async () => ({ maxPromptBytes: 200000, images: { maxCount: 12, maxInputBytes: 2e7, mediaTypes: ["image/jpeg", "image/png"] } });
  sample.json = async (prompt, opts = {}) => {
    const imgs = opts.images ? [].concat(opts.images instanceof FileList ? [...opts.images] : opts.images) : [];
    return (await ask({ prompt, images: await Promise.all(imgs.map(b64)) })).result;
  };
  const downloads = { save: async ({ filename, data }) => {
    const url = URL.createObjectURL(data instanceof Blob ? data : new Blob([data]));
    const a = Object.assign(document.createElement("a"), { href: url, download: filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    return { status: "saved" };
  } };
  window.claude = { use: async name => name === "sample" ? sample : name === "downloads" ? downloads : null };
})();
</script>
"""


@app.get("/")
def index():
    html = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E'
            '%3Crect width=%2732%27 height=%2732%27 rx=%277%27 fill=%27%23ff8a3d%27/%3E%3Cpath d=%27M12 9l11 7-11 7z%27 fill=%27%231a0e05%27/%3E%3C/svg%3E">'
            '<style>html,body{margin:0}</style></head><body>' + SHIM + PAGE.read_text(encoding="utf-8") + "</body></html>")
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.get("/whisper/<path:name>")
def whisper_file(name: str):
    resp = send_from_directory(WHISPER, name, max_age=30 * 24 * 3600)
    kind = TYPES.get(Path(name).suffix.lower())
    if kind:
        resp.headers["Content-Type"] = kind
    return resp


@app.get("/healthz")
def healthz():
    return "ok"


# ------------------------------------------------------------------------- settings


@app.get("/api/settings")
def get_settings():
    key = api_key()
    return jsonify(has_key=bool(key), key_hint=(key[:7] + "…" + key[-4:]) if len(key) > 12 else "",
                   from_env=bool(os.environ.get("ANTHROPIC_API_KEY")), model=model_name(),
                   workspace=bool(workspace_id()), captions_ready=assets.ready())


@app.post("/api/settings")
def set_settings():
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    for field in ("anthropic_api_key", "anthropic_workspace_id", "model"):
        if field in body:
            value = str(body[field] or "").strip()
            if value:
                cfg[field] = value
            else:
                cfg.pop(field, None)
    if cfg.get("anthropic_api_key") and not cfg["anthropic_api_key"].startswith("sk-ant-"):
        return jsonify(error="That doesn't look like an Anthropic API key (they start with sk-ant-)."), 400
    save_config(cfg)
    return get_settings()


# --------------------------------------------------------------------------- claude


def _parse_json(text: str):
    """The reply as JSON: whole text, else a fenced block, else first [/{ to last ]/}."""
    text = text.strip()
    for candidate in (text, *re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(text.rfind("]"), text.rfind("}"))
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
    raise ValueError("no JSON in reply")


@app.post("/api/claude")
def claude():
    import anthropic

    body = request.get_json(silent=True) or {}
    prompt = str(body.get("prompt") or "")
    images = body.get("images") or []
    if not prompt or len(prompt) > 200_000 or not isinstance(images, list) or len(images) > 12:
        return jsonify(code="invalid_request", error="Bad request."), 400
    key = api_key()
    if not key:
        return jsonify(code="no_key", error="Add your Claude API key to get Claude scoring."), 409

    content = []
    for img in images:
        if not isinstance(img, str) or len(img) > 8_000_000:
            return jsonify(code="invalid_request", error="Image too large."), 400
        try:
            head = base64.b64decode(img[:16] + "=" * (-len(img[:16]) % 4))
        except ValueError:
            return jsonify(code="invalid_request", error="Bad image."), 400
        kind = "image/png" if head.startswith(b"\x89PNG") else "image/jpeg"
        content.append({"type": "image", "source": {"type": "base64", "media_type": kind, "data": img}})
    content.append({"type": "text", "text": prompt})

    ws = workspace_id()
    client = anthropic.Anthropic(api_key=key, default_headers={"anthropic-workspace-id": ws} if ws else None,
                                 timeout=300, max_retries=2)
    request_args = dict(model=model_name(), max_tokens=16000, thinking={"type": "adaptive"},
                        messages=[{"role": "user", "content": content}])
    try:
        try:
            # Server-side fallbacks: if the model declines on policy grounds, the API
            # retries on a suitable model inside the same call.
            resp = client.beta.messages.create(**request_args, output_config={"effort": "medium"},
                                               betas=["server-side-fallback-2026-07-01"],
                                               extra_body={"fallbacks": "default"})
        except anthropic.BadRequestError as exc:
            msg = str(exc.message).lower()
            if any(w in msg for w in ("credit balance", "workspace")):
                raise
            # A model or account that doesn't take these newer options: ask plainly instead.
            resp = client.messages.create(**request_args)
    except anthropic.AuthenticationError:
        return jsonify(code="bad_key", error="Anthropic rejected this API key."), 401
    except anthropic.PermissionDeniedError as exc:
        return jsonify(code="bad_key", error=f"This API key isn't allowed to do that: {exc.message}"), 403
    except anthropic.RateLimitError:
        return jsonify(code="rate_limited", error="Claude is rate-limited right now."), 429
    except anthropic.BadRequestError as exc:
        msg = str(exc.message)
        if "credit balance" in msg.lower():
            return jsonify(code="no_credits", error="Your Anthropic account is out of credits."), 402
        if "workspace" in msg.lower():
            return jsonify(code="workspace", error="This key isn't tied to a workspace."), 400
        return jsonify(code="upstream_error", error=msg[:300]), 502
    except anthropic.APIStatusError as exc:
        return jsonify(code="upstream_error", error=f"Anthropic API error {exc.status_code}."), 502
    except anthropic.APIConnectionError:
        return jsonify(code="upstream_error", error="Couldn't reach the Anthropic API."), 502

    if resp.stop_reason == "refusal":
        return jsonify(code="refused", error="Claude declined to rate this video."), 422
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    if body.get("raw"):
        return jsonify(text=text, model=resp.model)
    try:
        return jsonify(result=_parse_json(text), model=resp.model)
    except ValueError:
        return jsonify(code="invalid_json", error="Claude's reply wasn't readable."), 502


# -------------------------------------------------------------------------- links


def _cleanup() -> None:
    root = WORK / "fetch"
    if KEEP_HOURS <= 0 or not root.exists():
        return
    cutoff = time.time() - KEEP_HOURS * 3600
    for folder in root.iterdir():
        job = fetches.get(folder.name)
        if job and job["state"] == "running":
            continue
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            fetches.pop(folder.name, None)


def _fetch(job_id: str, url: str, folder: Path) -> None:
    job = fetches[job_id]

    def progress(stage: str, pct: float) -> None:
        job["stage"], job["pct"] = stage, round(pct, 3)

    try:
        path, title = download(url, folder, progress)
        job.update(state="done", path=str(path), title=title, ext=path.suffix.lstrip(".") or "mp4",
                   size=path.stat().st_size)
    except Exception as exc:
        traceback.print_exc()
        job.update(state="error", error=str(exc)[:400])


@app.post("/api/fetch")
def start_fetch():
    url = str((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not is_url(url):
        return jsonify(error="Paste a full link that starts with https://"), 400
    _cleanup()
    job_id = uuid.uuid4().hex[:12]
    folder = WORK / "fetch" / job_id
    folder.mkdir(parents=True)
    fetches[job_id] = {"state": "running", "stage": "info", "pct": 0.0, "url": url}
    threading.Thread(target=_fetch, args=(job_id, url, folder), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/fetch/<job_id>")
def fetch_status(job_id: str):
    job = fetches.get(job_id) or abort(404)
    return jsonify({k: v for k, v in job.items() if k != "path"})


@app.get("/api/fetch/<job_id>/file")
def fetch_file(job_id: str):
    job = fetches.get(job_id) or abort(404)
    if job["state"] != "done":
        abort(409)
    ext = job["ext"].lower()
    kind = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "mov": "video/quicktime"}.get(ext)
    return send_file(job["path"], mimetype=kind or "application/octet-stream", conditional=True)


# ----------------------------------------------------------------------------- main


def _free_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port between {start} and {start + 19}.")


def main() -> None:
    host = os.environ.get("CLIP_HOST", "127.0.0.1")
    if host not in LOOPBACK and not PASSWORD:
        raise SystemExit("Refusing to serve beyond this computer without CLIP_PASSWORD.")
    assets.ensure()
    port = _free_port(host, int(os.environ.get("PORT", "8000")))
    url = f"http://localhost:{port}" if host in LOOPBACK else f"http://{host}:{port}"
    print(f"\n  Clipper is running at {url}\n  Leave this window open while you use it; close it to stop.\n", flush=True)
    if not os.environ.get("CLIP_NO_BROWSER"):
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # keep the window readable
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
