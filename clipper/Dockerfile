# Build from the repo root:  docker build -f clipper/Dockerfile .
FROM python:3.11-slim

# ffmpeg with libass for captions, a bold font for them, libgomp for whisper.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-liberation libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to download from YouTube.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY clipper/requirements.txt clipper/requirements.txt
RUN pip install --no-cache-dir -r clipper/requirements.txt gunicorn

# Bake the Whisper model into the image so the first video doesn't wait on a download.
ARG WHISPER_MODEL=small
ENV CLIP_WHISPER_MODEL=${WHISPER_MODEL} \
    HF_HOME=/app/models \
    CLIP_WORK_DIR=/data \
    CLIP_FONT="Liberation Sans" \
    PYTHONUNBUFFERED=1
RUN if [ "$WHISPER_MODEL" != "none" ]; then \
      python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"; \
    fi

COPY clipper clipper
VOLUME /data
EXPOSE 8000

# Optional cookies.txt for YouTube bot checks (see DEPLOY.md).
ENV CLIP_YTDLP_COOKIES=/data/cookies.txt

# One process on purpose: jobs live in memory, and one render already uses every core.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "600", "-b", "0.0.0.0:8000", "clipper.app:app"]
