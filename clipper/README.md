# Clipper

An OpusClip-style tool: give it a long video, get back the best **30-second
vertical (1080×1920) clip** with word-by-word captions burned in.

## Run it

```bash
pip install -r clipper/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # optional, but makes clip picks much smarter
python -m clipper.app                   # open http://localhost:8000
```

Or from the command line:

```bash
python -m clipper.pipeline my_podcast.mp4 -o clip.mp4
```

The first run downloads the Whisper speech model (~500 MB for `small`).

## What it does

1. **Transcribes** the audio with faster-whisper, keeping per-word timestamps.
2. **Picks the 30 seconds.** With an Anthropic credential, Claude reads the
   timestamped transcript and chooses the window with the strongest hook,
   a self-contained thought and a payoff, snapped to a sentence start. Without
   one (or if the call fails), a heuristic scores every window on loudness,
   dynamics, dead air, words per second and whether it starts/ends on a sentence.
3. **Reframes to 9:16.**
   - Landscape with a visible speaker → the crop follows the largest face,
     smoothed so it pans like a camera operator and cuts on speaker switches.
   - Landscape with no face found → full frame over a blurred copy of itself.
   - Already vertical → scaled to 1080×1920.
4. **Captions** 3 words at a time, uppercase, with the spoken word highlighted
   yellow (ASS subtitles burned in by ffmpeg/libass).

Videos 30 s or shorter are passed through whole.

## Settings (env vars)

| Variable | Default | What it does |
| --- | --- | --- |
| `CLIP_WHISPER_MODEL` | `small` | `tiny`/`base` are faster, `medium`/`large-v3` more accurate |
| `CLIP_CLAUDE_MODEL` | `claude-opus-5` | Model that picks the clip |
| `CLIP_DISABLE_LLM` | unset | Set to force the heuristic picker |
| `CLIP_FONT` | `Arial` | Caption font (any installed font name) |
| `CLIP_MAX_MB` | `4096` | Upload size limit |
| `CLIP_WORK_DIR` | `clipper/work` | Where uploads and clips are stored |
| `PORT` | `8000` | Web UI port |

## Limits vs. the real OpusClip

- One clip per video (OpusClip returns a ranked batch).
- Face tracking uses OpenCV's Haar detector: fast and dependency-free, but it
  misses profile shots. Swap in MediaPipe/YuNet if that matters for your footage.
- Jobs live in memory; restarting the server forgets them (files stay in `work/`).
- The web UI has no auth — run it locally, don't expose it to the internet as-is.
