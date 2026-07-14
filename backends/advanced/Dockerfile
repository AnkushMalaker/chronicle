# ============================================
# Builder stage - install dependencies
# ============================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Install system dependencies needed for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    git \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Copy dependency files first (cache-friendly)
COPY pyproject.toml uv.lock ./

# Build a project .venv from the lockfile (includes git deps like Graphiti)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra deepgram --extra galileo --extra benchmark --no-install-project


# ============================================
# notesmd-cli builder - vault note rename/move with backlink rewrite
# ============================================
# The memory agent uses `notesmd-cli move` to rename a person note and rewrite every
# [[wikilink]] to it across the vault in one shot (verified non-destructive), and the
# fork adds `create --template` (seed a note from a Templates/ note + {{date}}/{{title}}).
# Pinned to the commit vendored in untracked/; vendor/ in the repo means the build is hermetic.
FROM golang:1.25-bookworm AS notesmd-builder
ARG NOTESMD_REPO=https://github.com/AnkushMalaker/notesmd-cli
ARG NOTESMD_REF=2566ac2
RUN git clone ${NOTESMD_REPO} /src \
    && cd /src \
    && git checkout ${NOTESMD_REF} \
    && CGO_ENABLED=0 go build -trimpath -mod=vendor -o /out/notesmd-cli .


# ============================================
# Production stage
# ============================================
FROM python:3.12-slim-bookworm AS prod

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    curl \
    ffmpeg \
    ripgrep \
    libc++1 \
    && rm -rf /var/lib/apt/lists/*

# notesmd-cli on PATH so the memory agent auto-detects it (shutil.which).
COPY --from=notesmd-builder /out/notesmd-cli /usr/local/bin/notesmd-cli

WORKDIR /app

# Copy locked virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Source layout + venv defaults for runtime shells and commands
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONPATH=/app/src:/app

# Copy application code
COPY . .

# diarization_config.json (if present) is already included by "COPY . ." above.
# A separate optional-glob COPY ("json*") errors under buildah/podman when the
# file is absent, and is redundant under docker — so it's intentionally omitted.

# Copy and make startup script executable
COPY start.sh ./
RUN chmod +x start.sh

CMD ["./start.sh"]


# ============================================
# Dev/Test stage - includes test dependencies
# ============================================
FROM python:3.12-slim-bookworm AS dev

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    curl \
    ffmpeg \
    ripgrep \
    libc++1 \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# notesmd-cli on PATH so the memory agent auto-detects it (shutil.which).
COPY --from=notesmd-builder /out/notesmd-cli /usr/local/bin/notesmd-cli

WORKDIR /app

# For dev, create project .venv with test deps
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra deepgram --extra galileo --extra benchmark --group test --no-install-project && \
    rm /bin/uv /bin/uvx

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONPATH=/app/src:/app

# Copy application code
COPY . .

# diarization_config.json (if present) is already included by "COPY . ." above.
# A separate optional-glob COPY ("json*") errors under buildah/podman when the
# file is absent, and is redundant under docker — so it's intentionally omitted.

# Copy and make startup script executable
COPY start.sh ./
RUN chmod +x start.sh

CMD ["./start.sh"]
