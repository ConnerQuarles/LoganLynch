# Build from the repo root:  docker build -f clipper/Dockerfile .
# The same website as `python -m clipper.app`, for a server. Viewers' browsers do the
# transcription and rendering; the server fetches pasted links and calls Claude.
FROM python:3.11-slim

WORKDIR /app
COPY clipper/requirements.txt clipper/requirements.txt
RUN pip install --no-cache-dir -r clipper/requirements.txt gunicorn

COPY clipper clipper
# Bake the in-browser speech-to-text files into the image (~130 MB, from npm).
RUN python -m clipper.assets

ENV CLIP_WORK_DIR=/data \
    CLIP_CONFIG=/data/config.json \
    CLIP_YTDLP_COOKIES=/data/cookies.txt \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

# One process on purpose: link downloads are tracked in memory.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "600", "-b", "0.0.0.0:8000", "clipper.app:app"]
