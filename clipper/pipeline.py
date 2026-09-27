"""Clipper pipeline: long video in, ranked vertical 30-second highlights out.

Steps:
  1. Probe the source and pull a 16 kHz mono audio track.
  2. Transcribe with faster-whisper (word timestamps) if it's available.
  3. Pick the best non-overlapping 30 s windows and score each 0-100 — Claude
     reads the transcript when an API credential is configured, otherwise a
     loudness + speech-density heuristic ranks windows within the video.
  4. Reframe to 9:16: follow the speaker's face, or fall back to a
     blurred-background "fit" layout when no face is found.
  5. Burn in word-by-word captions and encode an H.264/AAC MP4.

Run it directly:  python -m clipper.pipeline input.mp4 -o clips/ -n 5
                  python -m clipper.pipeline https://youtu.be/... -o clips/
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import numpy as np

CLIP_SECONDS = 30.0
OUT_W, OUT_H = 1080, 1920
MAX_FPS = 30
ANALYSIS_FPS = 4
ANALYSIS_W = 640

Progress = Callable[[str, float], None]


def _noop(_stage: str, _pct: float) -> None:
    pass


# --------------------------------------------------------------------------- ffmpeg


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("ffmpeg not found: install it or `pip install imageio-ffmpeg`") from exc


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: Path) -> VideoInfo:
    # imageio-ffmpeg ships ffmpeg without ffprobe, so parse `ffmpeg -i` output.
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    err = proc.stderr
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    vid = re.search(r"Stream #.*?Video:.*?(\d{2,5})x(\d{2,5})", err)
    if not dur or not vid:
        raise RuntimeError("Couldn't read that file as a video.")
    h, m, s = dur.groups()
    duration = int(h) * 3600 + int(m) * 60 + float(s)
    width, height = int(vid.group(1)), int(vid.group(2))
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees|rotate\s*:\s*(-?\d+)", err)
    if rot:
        angle = abs(round(float(rot.group(1) or rot.group(2)))) % 180
        if angle == 90:  # ffmpeg auto-rotates on decode, so frames come out swapped
            width, height = height, width
    fps_m = re.search(r"(\d+(?:\.\d+)?) fps", err)
    fps = float(fps_m.group(1)) if fps_m else 30.0
    return VideoInfo(duration, width, height, fps, "Audio:" in err)


def extract_audio(src: Path, wav: Path) -> None:
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1",
                    "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True)


def loudness_curve(wav: Path, hop: float = 0.5) -> np.ndarray:
    """RMS loudness in dB, one value per `hop` seconds."""
    with wave.open(str(wav), "rb") as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    step = int(rate * hop)
    if step == 0 or samples.size < step:
        return np.zeros(1)
    frames = samples[: samples.size // step * step].reshape(-1, step)
    rms = np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-6
    return 20 * np.log10(rms / 32768)


# ------------------------------------------------------------------------ download


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
        # 1080p is plenty for a 1080x1920 crop and keeps downloads fast.
        "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
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


# ----------------------------------------------------------------------- transcript


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


def transcribe(wav: Path) -> list[Segment] | None:
    """Whisper transcript with word timings, or None if whisper isn't usable."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    size = os.environ.get("CLIP_WHISPER_MODEL", "small")
    try:
        model = WhisperModel(size, device="auto", compute_type="auto")
    except Exception:
        try:
            model = WhisperModel(size, device="cpu", compute_type="int8")
        except Exception as exc:  # model download blocked, etc.
            print(f"[clipper] whisper unavailable ({exc}); continuing without captions")
            return None
    segs, _info = model.transcribe(str(wav), word_timestamps=True, vad_filter=True)
    out = []
    for s in segs:
        words = [Word(w.start, w.end, w.word.strip()) for w in (s.words or []) if w.word.strip()]
        out.append(Segment(s.start, s.end, s.text.strip(), words))
    return out


# -------------------------------------------------------------------- clip picking


@dataclass
class Pick:
    start: float
    end: float
    title: str
    reason: str
    method: str
    score: int = 0   # overall virality, 0-100
    hook: int = 0    # does the first 3 s stop the scroll?
    flow: int = 0    # clean start/end, no dead air, stands alone
    value: int = 0   # payoff: insight, punchline, emotion


def _window_bounds(start: float, duration: float) -> tuple[float, float]:
    length = min(CLIP_SECONDS, duration)
    start = max(0.0, min(start, duration - length))
    return start, start + length


def _non_overlapping(picks: list[Pick], count: int, max_overlap: float = 2.0) -> list[Pick]:
    """Best-first greedy: keep a clip only if it barely overlaps the ones already kept."""
    kept: list[Pick] = []
    for p in sorted(picks, key=lambda p: p.score, reverse=True):
        if all(min(p.end, k.end) - max(p.start, k.start) <= max_overlap for k in kept):
            kept.append(p)
            if len(kept) == count:
                break
    return kept


def _pct_scores(values: np.ndarray) -> np.ndarray:
    """Percentile rank within this video, spread onto 35-95."""
    if values.size < 2:
        return np.full(values.size, 65.0)
    ranks = values.argsort().argsort() / (values.size - 1)
    return 35 + 60 * ranks


def pick_heuristic(duration: float, loud: np.ndarray, segments: list[Segment] | None,
                   count: int, hop: float = 0.5) -> list[Pick]:
    """Score every candidate window on energy, dead air and talk density.

    Scores are relative to the rest of this video (a percentile), not an absolute
    virality prediction — only the Claude picker judges content.
    """
    if duration <= CLIP_SECONDS:
        return [Pick(0.0, duration, "Full video", "Source is already 30 s or shorter.",
                     "heuristic", 65, 65, 65, 65)]

    # Candidates: every second, plus every sentence start so clips open cleanly.
    cands = set(np.arange(0.0, duration - CLIP_SECONDS + 0.01, 1.0).round(2).tolist())
    if segments:
        cands.update(round(s.start, 2) for s in segments if s.start <= duration - CLIP_SECONDS)
    word_times = np.array([w.start for s in (segments or []) for w in s.words])
    seg_starts = np.array([s.start for s in segments]) if segments else np.array([])
    seg_ends = np.array([s.end for s in segments]) if segments else np.array([])

    z = (loud - loud.mean()) / (loud.std() + 1e-6)
    win = int(CLIP_SECONDS / hop)
    starts, hooks, flows, values = [], [], [], []
    for start in sorted(cands):
        i = int(start / hop)
        chunk = z[i:i + win]
        if chunk.size < win * 0.8:
            continue
        # Loud, dynamic audio with few dead-air gaps reads as "something is happening".
        # Dynamics only count among the non-silent parts, so silence can't fake variety.
        live = chunk[chunk >= -1.5]
        dynamics = float(live.std()) if live.size > 1 else 0.0
        dead = float(np.mean(chunk < -1.5))
        hook = float(z[i:i + int(3 / hop)].mean())  # the first 3 seconds
        flow = -1.5 * dead
        value = chunk.mean() + 0.3 * dynamics
        if word_times.size:
            n = np.count_nonzero((word_times >= start) & (word_times < start + CLIP_SECONDS))
            value += 0.35 * (n / CLIP_SECONDS)  # ~2.5 words/s of speech ≈ +0.9
            if seg_starts.size and np.min(np.abs(seg_starts - start)) < 0.3:
                flow += 0.6  # opens on a sentence start
            if seg_ends.size and np.min(np.abs(seg_ends - (start + CLIP_SECONDS))) < 1.0:
                flow += 0.4  # ends near a sentence end
        starts.append(start)
        hooks.append(hook)
        flows.append(flow)
        values.append(value)
    if not starts:
        starts, hooks, flows, values = [0.0], [0.0], [0.0], [0.0]

    raw = 0.5 * np.array(hooks) + np.array(flows) + np.array(values)
    overall, hook_s, flow_s, value_s = (_pct_scores(np.array(v)) for v in (raw, hooks, flows, values))
    why = "High energy and dense speech for this video" if segments else "High audio energy for this video"
    picks = []
    for k, start in enumerate(starts):
        s, e = _window_bounds(start, duration)
        picks.append(Pick(s, e, "Highlight", why, "heuristic", int(overall[k]),
                          int(hook_s[k]), int(flow_s[k]), int(value_s[k])))
    picks = _non_overlapping(picks, count)
    for n, p in enumerate(picks, 1):
        p.title = f"Highlight #{n}"
    return picks


def pick_with_claude(duration: float, segments: list[Segment], count: int) -> list[Pick] | None:
    """Ask Claude for the most viral self-contained 30 s clips. None if unavailable."""
    if not segments or duration <= CLIP_SECONDS:
        return None
    if os.environ.get("CLIP_DISABLE_LLM"):
        return None
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        return None

    class ClipChoice(BaseModel):
        start_seconds: float
        title: str
        reason: str
        hook_score: int
        flow_score: int
        value_score: int
        virality_score: int

    class ClipList(BaseModel):
        clips: list[ClipChoice]

    lines = "\n".join(f"[{s.start:7.1f}-{s.end:7.1f}] {s.text}" for s in segments)
    prompt = (
        f"This is a timestamped transcript of a {duration:.0f}-second video. I'm cutting "
        f"up to {count} vertical shorts from it for TikTok/Reels/Shorts, each exactly "
        f"{CLIP_SECONDS:.0f} seconds long.\n\n"
        f"Find the {count} best clips. A great clip has a strong hook in the first 3 seconds, "
        "a complete thought that makes sense without the rest of the video, and a payoff "
        "(punchline, insight, reveal or strong emotion) before it ends. Each clip runs from "
        f"start_seconds to start_seconds + {CLIP_SECONDS:.0f}; start_seconds must be between "
        f"0 and {duration - CLIP_SECONDS:.1f} and land on the beginning of a sentence. "
        "Clips must not overlap. Return fewer clips if the video doesn't have enough good "
        "moments rather than padding with weak ones.\n\n"
        "Score each clip 0-100 on:\n"
        "- hook_score: would the first 3 seconds stop someone scrolling?\n"
        "- flow_score: does it start and end cleanly and stand on its own?\n"
        "- value_score: how strong is the payoff?\n"
        "- virality_score: overall likelihood it performs as a short.\n"
        "Calibrate honestly and use the whole range: 90+ is exceptional and rare, 70-89 is "
        "strong, 50-69 is usable, below 50 is weak. Don't inflate scores.\n\n"
        "Give each a punchy title (max 8 words) and a one-sentence reason for its score.\n\n"
        f"<transcript>\n{lines}\n</transcript>"
    )
    try:
        client = anthropic.Anthropic()
        resp = client.messages.parse(
            model=os.environ.get("CLIP_CLAUDE_MODEL", "claude-opus-5"),
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
            output_format=ClipList,
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return None
        choices = resp.parsed_output.clips
    except Exception as exc:  # no credentials, network, rate limit, ... → heuristic
        print(f"[clipper] Claude pick skipped ({type(exc).__name__}: {exc})")
        return None

    def pct(v: int) -> int:
        return int(max(0, min(100, v)))

    seg_starts = [s.start for s in segments]
    picks = []
    for c in choices:
        if not -1.0 <= c.start_seconds <= duration - CLIP_SECONDS + 2.0:
            continue  # hallucinated timestamp: clamping would score the wrong moment
        # Snap to the nearest sentence start so the clip doesn't open mid-word.
        snap = min(seg_starts, key=lambda t: abs(t - c.start_seconds))
        start = snap if abs(snap - c.start_seconds) < 2.0 else c.start_seconds
        s, e = _window_bounds(start, duration)
        picks.append(Pick(s, e, c.title, c.reason, "claude", pct(c.virality_score),
                          pct(c.hook_score), pct(c.flow_score), pct(c.value_score)))
    return _non_overlapping(picks, count) or None


# ----------------------------------------------------------------------- reframing


def _read_frames(src: Path, start: float, length: float, vf: str, w: int, h: int):
    cmd = [ffmpeg_exe(), "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
           "-i", str(src), "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        proc.wait()


def face_track(src: Path, info: VideoInfo, pick: Pick) -> np.ndarray | None:
    """Crop-centre x (0..1) sampled at ANALYSIS_FPS, or None if no steady face."""
    import cv2

    aw = ANALYSIS_W
    ah = int(round(info.height * aw / info.width / 2) * 2)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    xs, hits = [], 0
    for frame in _read_frames(src, pick.start, pick.end - pick.start,
                              f"fps={ANALYSIS_FPS},scale={aw}:{ah}", aw, ah):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(aw // 20, aw // 20))
        if len(faces):
            x, _y, w, _h = max(faces, key=lambda f: f[2] * f[3])  # biggest face = speaker
            xs.append((x + w / 2) / aw)
            hits += 1
        else:
            xs.append(np.nan)
    if not xs or hits / len(xs) < 0.3:
        return None
    arr = np.array(xs)
    # Fill gaps with the last seen position (first seen for leading gaps).
    first = arr[~np.isnan(arr)][0]
    for i in range(arr.size):
        if np.isnan(arr[i]):
            arr[i] = arr[i - 1] if i else first
    # Median kills detector jitter, then a slow EMA keeps the "camera" calm.
    k = 5
    padded = np.pad(arr, k // 2, mode="edge")
    arr = np.array([np.median(padded[i:i + k]) for i in range(arr.size)])
    for i in range(1, arr.size):
        if abs(arr[i] - arr[i - 1]) > 0.15:
            continue  # speaker switch: cut straight to them
        arr[i] = arr[i - 1] + 0.35 * (arr[i] - arr[i - 1])
    return arr


# ------------------------------------------------------------------------ captions


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def write_captions(segments: list[Segment], pick: Pick, path: Path, words_per_line: int = 3) -> bool:
    """Word-by-word highlighted captions (ASS). Returns False if there's nothing to show."""
    words = [w for s in segments for w in s.words if w.end > pick.start and w.start < pick.end]
    if not words:
        return False
    font = os.environ.get("CLIP_FONT", "Arial")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for i in range(0, len(words), words_per_line):
        group = words[i:i + words_per_line]
        for j, w in enumerate(group):
            t0 = max(w.start, pick.start) - pick.start
            # Hold each word until the next one starts so the line never blinks out.
            nxt = group[j + 1].start if j + 1 < len(group) else w.end + 0.15
            t1 = min(nxt, pick.end) - pick.start
            if t1 <= t0:
                continue
            parts = []
            for k, other in enumerate(group):
                txt = _ass_escape(other.text.upper())
                parts.append(r"{\c&H00E5FF&\fscx110\fscy110}" + txt + r"{\r}" if k == j else txt)
            events.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Cap,,0,0,0,,{' '.join(parts)}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return True


# -------------------------------------------------------------------------- render


def render(src: Path, info: VideoInfo, pick: Pick, track: np.ndarray | None,
           captions: Path | None, out: Path, progress: Progress = _noop) -> None:
    length = pick.end - pick.start
    fps = min(info.fps, MAX_FPS) if info.fps > 0 else MAX_FPS
    vertical = info.width / info.height <= OUT_W / OUT_H + 0.01

    post = f",subtitles={captions.name}" if captions else ""  # run with cwd=captions dir
    if vertical:
        # Already portrait: scale to fill, centre-crop any excess.
        vf = (f"fps={fps},scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
              f"crop={OUT_W}:{OUT_H},setsar=1{post}")
        _ffmpeg_render(src, pick, length, vf, out, captions, progress, fps)
        return

    if track is None:
        # No face: full frame over a blurred, zoomed copy of itself (OpusClip's "fit").
        fg_h = int(round(OUT_W * info.height / info.width / 2) * 2)
        vf = (f"fps={fps},split[a][b];"
              f"[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
              f"boxblur=24:2,eq=brightness=-0.12[bg];"
              f"[b]scale={OUT_W}:{fg_h}[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1{post}")
        _ffmpeg_render(src, pick, length, vf, out, captions, progress, fps)
        return

    # Face-tracked crop: Python moves the 9:16 window, ffmpeg does decode/encode.
    import cv2

    crop_w = int(info.height * OUT_W / OUT_H) // 2 * 2
    sample_t = np.arange(track.size) / ANALYSIS_FPS
    total = int(length * fps)
    enc = _encoder(src, pick, length, out, captions, fps, rawvideo=True)
    try:
        for k, frame in enumerate(_read_frames(src, pick.start, length, f"fps={fps}",
                                               info.width, info.height)):
            cx = float(np.interp(k / fps, sample_t, track)) * info.width
            x0 = int(np.clip(cx - crop_w / 2, 0, info.width - crop_w))
            crop = frame[:, x0:x0 + crop_w]
            enc.stdin.write(cv2.resize(crop, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR).tobytes())
            if k % 15 == 0:
                progress("render", min(k / max(total, 1), 1.0))
    finally:
        enc.stdin.close()
        if enc.wait() != 0:
            raise RuntimeError("ffmpeg failed while encoding the clip")


def _encoder(src: Path, pick: Pick, length: float, out: Path, captions: Path | None,
             fps: float, rawvideo: bool) -> subprocess.Popen:
    cmd = [ffmpeg_exe(), "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", f"{fps}", "-i", "-",
           "-ss", f"{pick.start:.3f}", "-t", f"{length:.3f}", "-i", str(src.resolve()),
           "-map", "0:v", "-map", "1:a?"]
    if captions:
        cmd += ["-vf", f"subtitles={captions.name}"]
    cmd += _codec_args(out)
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, cwd=captions.parent if captions else None)


def _codec_args(out: Path) -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(out.resolve())]


def _ffmpeg_render(src: Path, pick: Pick, length: float, vf: str, out: Path,
                   captions: Path | None, progress: Progress, fps: float) -> None:
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
           "-ss", f"{pick.start:.3f}", "-t", f"{length:.3f}", "-i", str(src.resolve()),
           "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "0:a?", *_codec_args(out)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True,
                            cwd=captions.parent if captions else None)
    for line in proc.stdout:
        if line.startswith("out_time_us="):
            try:
                progress("render", min(int(line.split("=")[1]) / 1e6 / length, 1.0))
            except ValueError:
                pass
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed while encoding the clip")


# ---------------------------------------------------------------------------- main


def make_clips(src: Path | str, outdir: Path, workdir: Path, progress: Progress = _noop,
               captions: bool = True, count: int = 5) -> dict:
    """Cut up to `count` non-overlapping 30 s clips, best score first.

    `src` is a local file or a video link. Writes clip_1.mp4 (highest score),
    clip_2.mp4, ... into `outdir`.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    title = None
    if is_url(str(src)):
        src, title = download(str(src), workdir, progress)
    src = Path(src)
    progress("probe", 0.0)
    info = probe(src)

    segments = None
    loud = np.zeros(max(1, int(info.duration / 0.5)))
    if info.has_audio:
        wav = workdir / "audio.wav"
        progress("audio", 0.0)
        extract_audio(src, wav)
        loud = loudness_curve(wav)
        progress("transcribe", 0.0)
        segments = transcribe(wav)

    progress("pick", 0.0)
    picks = pick_with_claude(info.duration, segments, count) if segments else None
    picks = picks or pick_heuristic(info.duration, loud, segments, count)
    picks.sort(key=lambda p: p.score, reverse=True)

    landscape = info.width / info.height > OUT_W / OUT_H + 0.01
    clips = []
    for i, pick in enumerate(picks):
        def clip_progress(_stage: str, pct: float, i: int = i) -> None:
            progress("render", (i + pct) / len(picks))

        clip_progress("render", 0.0)
        track = face_track(src, info, pick) if landscape else None
        cap_path = workdir / f"captions_{i + 1}.ass"
        has_caps = bool(captions and segments and write_captions(segments, pick, cap_path))
        out = outdir / f"clip_{i + 1}.mp4"
        render(src, info, pick, track, cap_path if has_caps else None, out, clip_progress)

        transcript = " ".join(w.text for s in (segments or []) for w in s.words
                              if pick.start <= w.start < pick.end)
        clips.append({
            "file": out.name,
            "start": round(pick.start, 2), "end": round(pick.end, 2),
            "title": pick.title, "reason": pick.reason,
            "score": pick.score, "hook": pick.hook, "flow": pick.flow, "value": pick.value,
            "layout": "vertical-source" if not landscape else ("face-tracked" if track is not None else "fit"),
            "captions": has_caps, "transcript": transcript,
        })
    progress("done", 1.0)
    return {"picked_by": picks[0].method, "duration": round(info.duration, 2),
            "source_title": title, "clips": clips}


def main() -> None:
    ap = argparse.ArgumentParser(description="Cut the best 30-second vertical clips from a video.")
    ap.add_argument("video", help="video file or link (YouTube, TikTok, Instagram, X, direct URL)")
    ap.add_argument("-o", "--out", type=Path, default=Path("clips"), help="output folder")
    ap.add_argument("-n", "--count", type=int, default=5, help="max number of clips")
    ap.add_argument("--no-captions", action="store_true")
    args = ap.parse_args()
    work = args.out / ".work"
    last = {"stage": None}

    def show(stage: str, pct: float) -> None:
        if stage != last["stage"]:
            print(f"-> {stage}")
            last["stage"] = stage

    video = args.video if is_url(args.video) else Path(args.video)
    result = make_clips(video, args.out, work, show, captions=not args.no_captions,
                        count=max(1, args.count))
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(result['clips'])} clips in {args.out}/ (scored by {result['picked_by']})")
    for c in result["clips"]:
        print(f"  {c['score']:3d}/100  {c['file']}  {c['start']:.0f}s-{c['end']:.0f}s  {c['title']}")
    (args.out / "clips.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
