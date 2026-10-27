FROM python:3.11-slim AS builder

# Wheels are built with their dependencies (no --no-deps): the runtime image
# installs offline from this directory, so anything missing here is missing at
# startup. A no-deps wheelhouse once produced an image that built, pushed and
# then died on `import sqlalchemy` because greenlet was never installed.
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels -r requirements.txt


FROM python:3.11-slim

# curl is used by the compose healthcheck; libportaudio2 is for any audio
# capture. espeak-ng data ships with the piper wheel, so no extra system TTS
# packages are needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/wheels /app/wheels
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-index --find-links=/app/wheels -r /app/requirements.txt \
    && rm -rf /app/wheels /app/requirements.txt

# The app resolves the frontend as <backend>/../frontend, so the two
# directories must keep their relative layout inside the image. Copying only
# backend/ leaves every page 404ing.
WORKDIR /app
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# Optional free speech engines. Left out of the default image because the
# voice models add ~175MB; enable by building with --build-arg WITH_SPEECH=1
# or by mounting a volume at /app/backend/models/piper.
ARG WITH_SPEECH=0
RUN if [ "$WITH_SPEECH" = "1" ]; then \
      pip install --no-cache-dir piper-tts faster-whisper && \
      python -m piper.download_voices en_GB-cori-high \
        --download-dir /app/backend/models/piper ; \
    fi

# Run unprivileged. The app writes only to the SQLite file (dev) and the
# HuggingFace cache, so give the user a home it can write to.
RUN useradd --create-home --uid 10001 vaani \
    && mkdir -p /app/backend/models /home/vaani/.cache \
    && chown -R vaani:vaani /app /home/vaani
USER vaani
ENV HOME=/home/vaani \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app/backend

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health/ready" || exit 1

# Migrations run at startup (AUTO_MIGRATE). --proxy-headers makes the app see
# the real scheme and client IP from the TLS proxy; it is safe because
# compose publishes the port on loopback only. PORT is honoured so the same
# image runs on Render/Fly/Cloud Run without an override.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips \"*\""]