# syntax=docker/dockerfile:1
#
# NVIDIA Audio Flamingo Next ASR Provider Dockerfile
#
# Promptable audio-language model with timestamped multi-talker ASR.
# Default checkpoint is the Think variant (emits <think>...</think>
# reasoning before the answer; we surface it via metadata + logs).
#
# License: NVIDIA OneWay Noncommercial — research use only.
# Roughly 18-24GB VRAM at BF16; fits on a single 3090/4090.

ARG PYTORCH_CUDA_VERSION=cu126

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg libsndfile1 git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
ARG PYTORCH_CUDA_VERSION
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --group af-next --extra ${PYTORCH_CUDA_VERSION}

ENV PATH="/app/.venv/bin:$PATH"

COPY common/ ./common/
COPY providers/af_next/ ./providers/af_next/

ENV HF_HOME="/models"
ENV ASR_MODEL="nvidia/audio-flamingo-next-hf"
ENV DEVICE="cuda"
ENV TORCH_DTYPE="bfloat16"
ENV MAX_NEW_TOKENS="4096"

EXPOSE 8765

CMD ["python", "-m", "providers.af_next.service", "--port", "8765"]
