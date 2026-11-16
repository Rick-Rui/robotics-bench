# CPU-only agent base for the separate Actions / RTX 4090 cohort.
# Task payload, GPU capabilities and provider credentials are injected per trial.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
LABEL org.opencontainers.image.source="https://github.com/ZIWU-AI/robotics-bench"
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash build-essential git curl ca-certificates jq sudo ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir numpy==1.26.0 pillow==11.3.0 \
    && useradd --create-home --uid 1000 agent \
    && mkdir -p /app/output /app/work /logs/agent /logs/verifier \
    && chown agent:agent /app/output /app/work /logs/agent \
    && chmod 1777 /app
ENV NPM_CONFIG_PREFIX=/home/agent/.local PYTHONUNBUFFERED=1
WORKDIR /app
CMD ["sleep", "infinity"]
