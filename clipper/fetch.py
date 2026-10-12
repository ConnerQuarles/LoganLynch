"""Download a video from a pasted link (YouTube, TikTok, Instagram, X, direct URL).

Kept separate from pipeline.py so the local website can use it without the
heavy video-processing dependencies.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

Progress = Callable[[str, float], None]


def _noop(_stage: str, _pct: float) -> None:
    pass


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("ffmpeg not found: install it or `pip install imageio-ffmpeg`") from exc


def is_url(value: str) -> bool:
    return str(value).lower().startswith(("http://", "https://"))


def _check_url(url: str) -> None:
    """Refuse links that point back into the server's own network."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("That doesn't look like a video link. Paste a full https:// URL.")
    if os.environ.get("CLIP_ALLOW_PRIVATE_URLS"):
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Couldn't find the site {parsed.hostname}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Links to private or local addresses aren't allowed.")


def download(url: str, workdir: Path, progress: Progress = _noop) -> tuple[Path, str]:
    """Fetch a video from YouTube/TikTok/Instagram/X/a direct link. Returns (file, title)."""
    import yt_dlp

    _check_url(url)
    max_min = float(os.environ.get("CLIP_MAX_MINUTES", "180"))
    max_mb = int(os.environ.get("CLIP_MAX_MB", "4096"))

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress("download", min(d.get("downloaded_bytes", 0) / total, 1.0))

    opts = {
        # H.264 + AAC first: every browser can play and decode it. 1080p is plenty
        # for a vertical crop and keeps downloads fast.
        "format": ("bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/b[vcodec^=avc1][height<=1080]"
                   "/bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"),
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_exe(),
        "max_filesize": max_mb * 1024 * 1024,
        "progress_hooks": [hook],
    }
    cookies = os.environ.get("CLIP_YTDLP_COOKIES")
    if cookies and Path(cookies).is_file():
        opts["cookiefile"] = cookies  # lets YouTube downloads through when it bot-checks the server
    progress("download", 0.0)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise RuntimeError(_friendly_download_error(str(exc))) from exc
        if info.get("_type") == "playlist":
            raise ValueError("That's a playlist. Paste a link to a single video.")
        if info.get("is_live"):
            raise ValueError("Live streams can't be clipped until they've ended.")
        duration = info.get("duration") or 0
        if duration > max_min * 60:
            raise ValueError(f"That video is {duration / 60:.0f} minutes; the limit is {max_min:.0f}.")
        try:
            info = ydl.process_ie_result(info, download=True)
        except yt_dlp.utils.DownloadError as exc:
            raise RuntimeError(_friendly_download_error(str(exc))) from exc
    files = [f for f in workdir.glob("source.*") if f.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("The download finished but no video file came out. Try another link.")
    return max(files, key=lambda f: f.stat().st_size), str(info.get("title") or "video")


def _friendly_download_error(msg: str) -> str:
    low = msg.lower()
    if "confirm you" in low and "bot" in low:
        return ("YouTube is blocking downloads from this server (bot check). Upload the file "
                "instead, or see DEPLOY.md > YouTube cookies.")
    if "private" in low or "login" in low or "sign in" in low:
        return "That video is private or needs a login, so it can't be downloaded."
    if "unsupported url" in low:
        return "That site isn't supported. Try a YouTube, TikTok, Instagram, X or direct video link."
    if "not available" in low or "404" in low:
        return "That video isn't available (deleted, region-locked, or the link is wrong)."
    return "Couldn't download that video: " + msg.replace("ERROR: ", "")[:300]
