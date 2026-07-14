# syntax=docker/dockerfile:1

# Fish Speech TTS provider for Chronicle.
#
# Follows fish-speech's own Docker pattern: clone → uv sync → uv run.
# Single stage keeps venv intact so fish-speech is properly registered
# as a package (no broken imports from multi-stage site-packages copy).
#
# Our wrapper code lives under chronicle/ to avoid conflicts with
# fish-speech's own modules (tools/, fish_speech/).

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG PYTORCH_CUDA_VERSION=cu126

# System deps (matching fish-speech's own Dockerfile)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsox-dev build-essential cmake portaudio19-dev libportaudio2 \
        ffmpeg git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone fish-speech and install with its own pyproject.toml
RUN git clone --depth 1 https://github.com/fishaudio/fish-speech.git /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra ${PYTORCH_CUDA_VERSION}

# Install our additional deps into the same venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install requests soundfile

# Copy our wrapper code under chronicle/ to avoid import conflicts
COPY common/ ./chronicle/common/
COPY providers/__init__.py ./chronicle/providers/__init__.py
COPY providers/fish_speech/ ./chronicle/providers/fish_speech/

ENV PYTHONUNBUFFERED=1
ENV TTS_MODEL="fishaudio/s2-pro"
EXPOSE 8770

# Our startup script handles model download + server orchestration
CMD ["uv", "run", "python", "chronicle/providers/fish_speech/startup.py"]
