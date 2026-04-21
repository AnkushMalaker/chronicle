# syntax=docker/dockerfile:1
#
# VibeVoice ASR Provider Dockerfile
#
# Uses transformers-based approach for Microsoft VibeVoice-ASR.
# VibeVoice provides speech-to-text with speaker diarization.

#########################  builder #################################
# NVIDIA CUDA devel image provides nvcc + headers needed to compile
# flash-attn CUDA kernels.  Ubuntu 24.04 ships Python 3.12.
FROM nvidia/cuda:12.6.3-devel-ubuntu24.04 AS builder

ARG PYTORCH_CUDA_VERSION=cu126

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3.12-venv \
        build-essential ninja-build git \
    && rm -rf /var/lib/apt/lists/*

# Grab uv binary from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV CUDA_HOME=/usr/local/cuda

# Dependency manifest first for cache-friendly installs
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --group vibevoice --extra ${PYTORCH_CUDA_VERSION}

# flash-attn compiles CUDA kernels against the toolkit above
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install flash-attn --no-build-isolation

#########################  runtime #################################
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg libsndfile1 gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder's venv to system Python
COPY --from=builder /app/.venv/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# Copy application code
COPY common/ ./common/
COPY providers/vibevoice/ ./providers/vibevoice/

# Default environment variables
ENV HF_HOME="/models"
ENV ASR_MODEL="microsoft/VibeVoice-ASR-HF"
ENV VIBEVOICE_ATTN_IMPL="flash_attention_2"
ENV FLASH_ATTENTION_DETERMINISTIC="1"
ENV DEVICE="cuda"
ENV TORCH_DTYPE="bfloat16"
ENV MAX_NEW_TOKENS="8192"
ENV QUANTIZATION="4bit"

EXPOSE 8765

CMD ["python", "-m", "providers.vibevoice.service", "--port", "8765"]
