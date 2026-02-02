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
RUN uv sync --no-install-project --group transformers && \
    uv cache clean

#########################  runtime #################################
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment and application
COPY --from=builder /app /app
COPY common/ ./common/
COPY providers/transformers/ ./providers/transformers/

ENV PATH="/app/.venv/bin:$PATH"

# Default environment variables
ENV ASR_MODEL="openai/whisper-large-v3"
ENV USE_FLASH_ATTENTION="false"
ENV DEVICE="cuda"
ENV TORCH_DTYPE="float16"

EXPOSE 8765

CMD ["python", "-m", "providers.transformers.service", "--port", "8765"]
