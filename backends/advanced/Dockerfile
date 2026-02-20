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

# Export locked deps to requirements.txt (handles extras, git sources, custom indexes)
# Install to system Python (no venv) - container IS the isolation
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev --extra deepgram --extra galileo --no-emit-project -o requirements.txt && \
    uv pip install --system -r requirements.txt


# ============================================
# Production stage
# ============================================
FROM python:3.12-slim-bookworm AS prod

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Source layout needs PYTHONPATH
ENV PYTHONPATH=/app/src

# Copy application code
COPY . .

# Copy configuration files if they exist
COPY diarization_config.json* ./

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
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# For dev, install deps + test group using uv temporarily
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --extra deepgram --extra galileo --group test --no-emit-project -o requirements.txt && \
    uv pip install --system -r requirements.txt && \
    rm /bin/uv /bin/uvx

ENV PYTHONPATH=/app/src

# Copy application code
COPY . .

# Copy configuration files if they exist
COPY diarization_config.json* ./

# Copy and make startup script executable
COPY start.sh ./
RUN chmod +x start.sh

CMD ["./start.sh"]
