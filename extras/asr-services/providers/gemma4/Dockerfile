# syntax=docker/dockerfile:1
#
# Gemma 4 E2B-it ASR Provider Dockerfile
#
# Google Gemma 4 multimodal model for speech-to-text with prompt-based diarization.
# Default target is the E2B-it model (~5.1B params, ~10GB bf16 incl. the 78M MTP
# assistant drafter) so it runs full precision on a single 24GB GPU. Set
# GEMMA4_QUANT=4bit/8bit for smaller GPUs.

ARG PYTORCH_CUDA_VERSION=cu126

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime deps + uv
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install dependencies into .venv
COPY pyproject.toml uv.lock ./
ARG PYTORCH_CUDA_VERSION
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --group gemma4 --extra ${PYTORCH_CUDA_VERSION}

# Activate venv by prepending to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Copy application code
COPY common/ ./common/
COPY providers/gemma4/ ./providers/gemma4/

# Default environment
ENV HF_HOME="/models"
ENV ASR_MODEL="google/gemma-4-E2B-it"
ENV DEVICE="cuda"
ENV TORCH_DTYPE="bfloat16"
ENV MAX_NEW_TOKENS="512"
ENV GEMMA4_QUANT="bf16"
ENV GEMMA4_MTP="1"

EXPOSE 8765

CMD ["python", "-m", "providers.gemma4.service", "--port", "8765"]
