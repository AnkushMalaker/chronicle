# syntax=docker/dockerfile:1

#########################  builder #################################
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Accept PyTorch CUDA version as build argument
ARG PYTORCH_CUDA_VERSION=cu126
# Optional: overlay an unreleased NeMo from git on top of the stable deps. The
# Nemotron 3.5 prompt-conditioned RNNT class (EncDecRNNTBPEModelWithPrompt) only
# exists on NeMo main as of 2026-06. Empty = stable PyPI NeMo (Parakeet/Canary).
ARG NEMO_GIT_REF=

WORKDIR /app

# NeMo and texterrors need libs and C++ compiler
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
        build-essential git portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Dependency manifest first for cache-friendly installs
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --group nemo --extra ${PYTORCH_CUDA_VERSION}

# Overlay an unreleased NeMo from git when NEMO_GIT_REF is set. Install WITH deps
# so NeMo main's bumped requirements (lhotse, etc.) are reconciled, but constrain
# torch/torchaudio to the CUDA build the stable group already installed so they
# aren't pulled from the CPU index. uv pip reads the package METADATA (plain
# requirements), so NeMo's own CPU-torch index config never enters resolution.
ARG NEMO_GIT_REF
ENV VIRTUAL_ENV=/app/.venv
# uvicorn needs a WebSocket backend to serve the /stream endpoint; the nemo
# group doesn't pull one in. (Not in the lock, so install it directly here.)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install websockets
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "${NEMO_GIT_REF}" ]; then \
      printf 'torch==2.9.1+cu126\ntorchaudio==2.9.1+cu126\n' > /tmp/torch-constraints.txt && \
      uv pip install \
        --index-strategy unsafe-best-match \
        --extra-index-url https://download.pytorch.org/whl/cu126 \
        -c /tmp/torch-constraints.txt \
        "nemo-toolkit[asr] @ git+https://github.com/NVIDIA/NeMo.git@${NEMO_GIT_REF}"; \
    fi

#########################  runtime #################################
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 build-essential portaudio19-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder's venv to system Python
COPY --from=builder /app/.venv/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# Copy application code
COPY common/ ./common/
COPY providers/nemo/ ./providers/nemo/

# Default environment variables
ENV ASR_MODEL="nvidia/parakeet-tdt-0.6b-v3"
ENV CHUNKING_ENABLED="true"
ENV MIN_AUDIO_FOR_CHUNKING="60.0"
ENV CHUNK_DURATION_SECONDS="30.0"

EXPOSE 8765

CMD ["python", "-m", "providers.nemo.service", "--port", "8765"]
