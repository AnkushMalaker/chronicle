# syntax=docker/dockerfile:1
#
# IBM Granite Speech ASR Provider Dockerfile
#
# IBM Granite Speech multimodal model (Granite LLM backbone + conformer speech
# encoder + built-in LoRA adapter) for speech-to-text. Default target is the
# granite-speech-4.1-2b-plus model (~2B params), which runs in bf16 on a single
# GPU. Supports English, French, German, Spanish, Portuguese.

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
    uv sync --frozen --no-install-project --group granite --extra ${PYTORCH_CUDA_VERSION}

# Activate venv by prepending to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Copy application code
COPY common/ ./common/
COPY providers/granite/ ./providers/granite/

# Default environment
ENV HF_HOME="/models"
ENV ASR_MODEL="ibm-granite/granite-speech-4.1-2b-plus"
ENV DEVICE="cuda"
ENV TORCH_DTYPE="bfloat16"
ENV MAX_NEW_TOKENS="512"

EXPOSE 8765

CMD ["python", "-m", "providers.granite.service", "--port", "8765"]
