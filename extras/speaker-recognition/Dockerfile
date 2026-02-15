# ============================================
# Builder - install dependencies
# ============================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ARG PYTORCH_CUDA_VERSION=cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./

# Create minimal package structure for uv sync
RUN mkdir -p src/simple_speaker_recognition
COPY src/simple_speaker_recognition/__init__.py src/simple_speaker_recognition/

# uv sync handles custom PyTorch indexes natively
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ${PYTORCH_CUDA_VERSION}

# ============================================
# Runtime
# ============================================
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder's venv to system Python
COPY --from=builder /app/.venv/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# Copy source
COPY src/ src/

RUN mkdir -p /app/audio_chunks /app/debug /app/data /models
ENV HF_HOME=/models
ENV PYTHONPATH=/app

EXPOSE 8085

# Use python -m instead of entry point scripts (avoids venv shebang issues)
CMD ["python", "-m", "simple_speaker_recognition.api.service"]
