FROM python:3.12-slim-bookworm AS builder

# Install system dependencies for building
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    git \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.6.10 /uv /uvx /bin/

# Set up the working directory
WORKDIR /app

# chronicle-client arrives through a separate build context so this image's
# main context stays limited to extras/havpe-relay (see docker-compose.yml).
# /chronicle-client is ../chronicle-client relative to /app, which is exactly
# what pyproject.toml declares on the host — one spec works in both places.
COPY --from=chronicleclient . /chronicle-client

# Copy dependency files
COPY pyproject.toml .
COPY uv.lock .

# Install dependencies using uv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync

# Copy application code
COPY . .

# Expose the TCP port that the relay listens on
EXPOSE 8989

# Run the application
CMD ["uv", "run", "python3", "main.py"]
