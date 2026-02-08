# syntax=docker/dockerfile:1
#
# VibeVoice ASR Provider Dockerfile
#
# Uses transformers-based approach for Microsoft VibeVoice-ASR.
# VibeVoice provides speech-to-text with speaker diarization.

#########################  builder #################################
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Install system dependencies for building
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
    && rm -rf /var/lib/apt/lists/*

# Dependency manifest first for cache-friendly installs
COPY pyproject.toml uv.lock ./
RUN uv sync --no-install-project --group vibevoice && \
    uv cache clean

#########################  runtime #################################
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg git libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application code
COPY common/ ./common/
COPY providers/vibevoice/ ./providers/vibevoice/

# Pre-clone VibeVoice repository for faster startup
ENV HF_HOME="/models"
RUN mkdir -p /models && \
    git clone https://github.com/microsoft/VibeVoice.git /models/vibevoice

# Default environment variables
ENV ASR_MODEL="microsoft/VibeVoice-ASR"
ENV VIBEVOICE_LLM_MODEL="Qwen/Qwen2.5-7B"
ENV VIBEVOICE_ATTN_IMPL="sdpa"
ENV DEVICE="cuda"
ENV TORCH_DTYPE="bfloat16"
ENV MAX_NEW_TOKENS="8192"

EXPOSE 8765

CMD ["python", "-m", "providers.vibevoice.service", "--port", "8765"]
