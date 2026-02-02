# syntax=docker/dockerfile:1

#########################  builder #################################
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
    && rm -rf /var/lib/apt/lists/*

# Dependency manifest first for cache-friendly installs
COPY pyproject.toml uv.lock ./
RUN uv sync --no-install-project --group faster-whisper && \
    uv cache clean

#########################  runtime #################################
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment and application
COPY --from=builder /app /app
COPY common/ ./common/
COPY providers/faster_whisper/ ./providers/faster_whisper/

ENV PATH="/app/.venv/bin:$PATH"

# Default environment variables
ENV ASR_MODEL="Systran/faster-whisper-large-v3"
ENV COMPUTE_TYPE="float16"
ENV DEVICE="cuda"
ENV VAD_FILTER="true"

EXPOSE 8765

CMD ["python", "-m", "providers.faster_whisper.service", "--port", "8765"]
