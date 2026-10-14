# syntax=docker/dockerfile:1
# --- build stage: install deps (aiohttp, sgp4) with pip; the runtime stage below has no shell/pip.
FROM cgr.dev/chainguard/python:latest-dev AS build
USER root
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile --target /deps -r requirements.txt
COPY uksitrep /app/uksitrep
COPY config /app/config
RUN mkdir -p /data

# --- runtime: Chainguard Python (distroless, no shell, patched continuously), runs as uid 65532
FROM cgr.dev/chainguard/python:latest
COPY --from=build /deps /deps
COPY --from=build /app /app
COPY --from=build --chown=65532:65532 /data /data
ENV PYTHONPATH=/deps:/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8080
WORKDIR /app
EXPOSE 8080
VOLUME ["/data"]
USER 65532:65532
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)"]
ENTRYPOINT ["python", "-m", "uksitrep"]
