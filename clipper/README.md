# Clipper

An OpusClip-style tool: give it a long video (or paste a YouTube/TikTok link) and
get back its best moments as vertical 9:16 clips with word-by-word captions, each
**rated out of 100**, ready to download and post.

## Run it on your computer

1. Install [Python 3.10+](https://www.python.org/downloads/) (on Windows, tick
   **"Add python.exe to PATH"**).
2. Double-click **`start-clipper.bat`** (Windows) or **`start-clipper.command`**
   (Mac) in the repo root. On Linux: `./start-clipper.sh`.

The first run installs everything and sets up speech-to-text (a few minutes,
~130 MB); after that it starts in seconds and opens
[http://localhost:8000](http://localhost:8000). Keep the window open while you use
it; close it to stop.

To get Claude scoring, paste your Anthropic API key into the **Claude API key**
panel on the page (saved in `~/.clipper/config.json`, only on your computer).
It costs roughly 5–15¢ per video.

Manual start, if you prefer: `pip install -r clipper/requirements.txt` then
`python -m clipper.app`.

## What the website does

- **Paste a link** (YouTube, TikTok, Instagram, X, direct video URL) and your
  computer downloads it with yt-dlp, or drop in a video file.
- **Video types** (Podcast, Gaming, Sports, Comedy, Education, Vlog, Music) change
  how moments are scored, what Claude looks for and the caption defaults.
- **Finds and scores moments**: audio energy, spikes and dead air pick a
  shortlist; Whisper transcribes it in your browser; Claude scores each moment
  0-100 (Hook / Flow / Value) from the transcript and frames, writes a hook title
  and a ready-to-post caption, and picks caption keywords and emojis.
- **Clip length** 15 / 30 / 45 / 60 s; nudge any clip 5 s earlier or later.
- **Vertical layouts**: Fit + blur, Fill with smart crop that follows the person,
  or Split (facecam on top, gameplay below; the facecam corner is detected).
- **Captions**: Pop / Box / Clean styles, 1–4 words at a time, lower or middle,
  editable text, keyword colors and emojis, optional hook title and progress bar.
- **Download and post**: download each clip (or all), then one tap copies the
  caption and opens TikTok / Instagram / YouTube Shorts / X upload.

The same page also runs as a claude.ai artifact (`browser/clipper.html`); there it
can't fetch links and uses the viewer's Claude account instead of an API key.

### Settings (environment variables)

| Variable | Default | What it does |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Use this key instead of the one saved on the page |
| `ANTHROPIC_WORKSPACE_ID` | unset | Only for org-level keys that aren't tied to a workspace |
| `CLIP_CLAUDE_MODEL` | `claude-opus-5` | Model that scores clips |
| `PORT` | `8000` | Port (the next free one is used if it's taken) |
| `CLIP_NO_BROWSER` | unset | Don't open the browser on start |
| `CLIP_YTDLP_COOKIES` | unset | Path to a cookies.txt if YouTube asks to confirm you're not a bot |
| `CLIP_MAX_MINUTES` | `180` | Longest video a pasted link may be |
| `CLIP_KEEP_HOURS` | `24` | Delete downloaded videos older than this |
| `CLIP_PASSWORD` | unset | Required to serve beyond this computer (with `CLIP_HOST=0.0.0.0`) |

Without `CLIP_PASSWORD` the site only answers requests addressed to localhost, so
other devices (or web pages) can't spend your Claude credits.

## Put it online

See [DEPLOY.md](DEPLOY.md) for a password-protected HTTPS deploy on a Hostinger
VPS (works alongside an existing n8n/Traefik setup).

## Command line and GitHub Actions pipeline

A server-side version of the pipeline (faster-whisper, OpenCV face tracking,
ffmpeg rendering) is still available:

```bash
pip install -r clipper/requirements-pipeline.txt
python -m clipper.pipeline my_podcast.mp4 -o clips/ -n 5
python -m clipper.pipeline "https://www.youtube.com/watch?v=..." -o clips/
```

`.github/workflows/clipper.yml` runs it on GitHub Actions when a request file is
committed to `clipper/jobs/requests/` (with `docs/index.html` as a front end);
`clipper-selftest.yml` checks it end to end. On GitHub's runners YouTube needs a
`YT_COOKIES` secret.

## Files

| Path | What |
| --- | --- |
| `app.py` | The website (serves the page, link downloads, Claude calls) |
| `browser/clipper.html` | The page itself (same file as the claude.ai artifact) |
| `browser/whisper/asr-worker.js` | In-browser speech-to-text worker |
| `assets.py` | Builds the speech-to-text files from npm on first start |
| `fetch.py` | Link downloads (yt-dlp) |
| `pipeline.py`, `gh_job.py` | Command-line / GitHub Actions pipeline |
