# syntax=docker/dockerfile:1
# --- build stage: install the one dependency (aiohttp) for the distroless Python (3.11)
FROM python:3.11-slim-bookworm AS build
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-compile --target /deps -r /tmp/requirements.txt
COPY uksitrep /app/uksitrep
COPY config /app/config
RUN mkdir -p /data

# --- runtime: distroless, no shell, runs as uid 65532
FROM gcr.io/distroless/python3-debian12:nonroot
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
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD ["python3", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)"]
ENTRYPOINT ["python3", "-m", "uksitrep"]
