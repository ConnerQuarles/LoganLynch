# Clipper

An OpusClip-style tool: give it a long video, get back its best **30-second
vertical (1080×1920) clips**, each **rated out of 100**, with word-by-word
captions burned in.

## Run it

```bash
pip install -r clipper/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # optional, but makes clip picks much smarter
python -m clipper.app                   # open http://localhost:8000
```

Or from the command line:

```bash
python -m clipper.pipeline my_podcast.mp4 -o clips/ -n 5
python -m clipper.pipeline "https://www.youtube.com/watch?v=..." -o clips/
# -> clips/clip_1.mp4 (highest score) ... plus clips/clips.json with scores
```

The first run downloads the Whisper speech model (~500 MB for `small`).

## Put it online

See [DEPLOY.md](DEPLOY.md) for a password-protected HTTPS deploy on a
Hostinger VPS (works alongside an existing n8n/Traefik setup).

## What it does

0. **Downloads** the video first if you pasted a link (YouTube, TikTok,
   Instagram, X or a direct URL) instead of uploading, using yt-dlp.
1. **Transcribes** the audio with faster-whisper, keeping per-word timestamps.
2. **Picks and scores the clips** (default 5, max 10, never overlapping,
   best first). Each gets a 0-100 **virality score** plus **Hook** (does the
   first 3 s stop the scroll), **Flow** (clean start/end, no dead air, stands
   alone) and **Value** (strength of the payoff).
   - With an Anthropic credential, Claude reads the timestamped transcript,
     chooses the moments and scores them on a calibrated scale (90+ rare,
     70-89 strong, 50-69 usable). Timestamps it invents outside the video are
     discarded; starts are snapped to sentence boundaries.
   - Without one (or if the call fails), a heuristic scores every window on
     loudness, dynamics, dead air, words per second and sentence alignment.
     **These scores are percentiles within the video** — a 95 means "better
     than almost every other window in this video", not "will go viral".
3. **Reframes to 9:16.**
   - Landscape with a visible speaker → the crop follows the largest face,
     smoothed so it pans like a camera operator and cuts on speaker switches.
   - Landscape with no face found → full frame over a blurred copy of itself.
   - Already vertical → scaled to 1080×1920.
4. **Captions** 3 words at a time, uppercase, with the spoken word highlighted
   yellow (ASS subtitles burned in by ffmpeg/libass).

Videos 30 s or shorter come back as a single clip. A video can't yield more
clips than it has non-overlapping 30 s windows.

## Settings (env vars)

| Variable | Default | What it does |
| --- | --- | --- |
| `CLIP_WHISPER_MODEL` | `small` | `tiny`/`base` are faster, `medium`/`large-v3` more accurate |
| `CLIP_CLAUDE_MODEL` | `claude-opus-5` | Model that picks the clip |
| `CLIP_DISABLE_LLM` | unset | Set to force the heuristic picker |
| `CLIP_FONT` | `Arial` | Caption font (any installed font name) |
| `CLIP_MAX_MB` | `4096` | Upload size limit |
| `CLIP_WORK_DIR` | `clipper/work` | Where uploads and clips are stored |
| `CLIP_PASSWORD` | unset | Site password (browser login, any username). Required by the Docker deploy |
| `CLIP_MAX_MINUTES` | `180` | Longest video a pasted link may be |
| `CLIP_YTDLP_COOKIES` | unset | Path to a cookies.txt for YouTube bot checks |
| `CLIP_ALLOW_PRIVATE_URLS` | unset | Allow links to local/private addresses (off for safety) |
| `CLIP_KEEP_HOURS` | `24` | Delete uploads/clips older than this (`0` = keep forever) |
| `PORT` | `8000` | Web UI port |

## Limits vs. the real OpusClip

- Clips are a fixed 30 s; OpusClip varies length to fit the moment.
- Scores are Claude's judgment of the transcript, not a model trained on
  real view counts — treat them as a ranking, not a prediction.
- Face tracking uses OpenCV's Haar detector: fast and dependency-free, but it
  misses profile shots. Swap in MediaPipe/YuNet if that matters for your footage.
- Jobs live in memory; restarting the server forgets them (files stay in `work/`).
- Jobs run one at a time; a second upload waits in line.
