# syntax=docker/dockerfile:1

#########################  builder #################################
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Dependency manifest first for cache-friendly installs
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --group transformers

#########################  runtime #################################
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder's venv to system Python
COPY --from=builder /app/.venv/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# Copy application code
COPY common/ ./common/
COPY providers/transformers/ ./providers/transformers/

# Default environment variables
ENV ASR_MODEL="openai/whisper-large-v3"
ENV USE_FLASH_ATTENTION="false"
ENV DEVICE="cuda"
ENV TORCH_DTYPE="float16"

EXPOSE 8765

CMD ["python", "-m", "providers.transformers.service", "--port", "8765"]
