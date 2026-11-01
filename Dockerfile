# ATV Player media server (server/). Linux Docker hosts; see compose.yaml and README ("Sound through atv-audio", "Where it runs").
FROM python:3.12-slim

# ffmpeg/ffprobe: frame grabs, keyframes, audio copies, loudness. util-linux (ionice) is in the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir aiohttp mutagen pillow zeroconf

RUN useradd --system --uid 10001 --user-group --home-dir /data --no-create-home atvmedia \
 && mkdir -p /data /cache \
 && chown atvmedia:atvmedia /data /cache

WORKDIR /opt/atv-media
COPY server/*.py ./
COPY server/static ./static

# Library folders are mounted read-only below /media; ATV_ROOTS (env file) points at them.
ENV ATV_DATA=/data \
    ATV_CACHE=/cache \
    ATV_PORT=8080 \
    ATV_BONJOUR=1 \
    PYTHONUNBUFFERED=1
VOLUME ["/data", "/cache"]
EXPOSE 8080

USER atvmedia
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/state' % os.environ['ATV_PORT'], timeout=4)"
CMD ["python3", "/opt/atv-media/server.py"]
