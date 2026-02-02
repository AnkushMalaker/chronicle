# syntax=docker/dockerfile:1

#########################  builder #################################
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Accept PyTorch CUDA version as build argument
ARG PYTORCH_CUDA_VERSION=cu126

WORKDIR /app

# NeMo and texterrors need libs and C++ compiler
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
        build-essential git portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependency manifest first for cache-friendly installs
COPY pyproject.toml uv.lock ./
RUN uv sync --no-install-project --group nemo --extra ${PYTORCH_CUDA_VERSION} && \
    uv cache clean

#########################  runtime #################################
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 build-essential portaudio19-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment and application
COPY --from=builder /app /app
COPY common/ ./common/
COPY providers/nemo/ ./providers/nemo/

ENV PATH="/app/.venv/bin:$PATH"

# Default environment variables
ENV ASR_MODEL="nvidia/parakeet-tdt-0.6b-v3"
ENV CHUNKING_ENABLED="true"
ENV MIN_AUDIO_FOR_CHUNKING="60.0"
ENV CHUNK_DURATION_SECONDS="30.0"

EXPOSE 8765

CMD ["python", "-m", "providers.nemo.service", "--port", "8765"]
