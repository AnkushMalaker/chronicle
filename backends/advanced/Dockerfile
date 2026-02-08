# ============================================
# Base stage - common setup
# ============================================
FROM python:3.12-slim-bookworm AS base

# Install system dependencies
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

# Set up working directory
WORKDIR /app

# Copy package structure and dependency files
COPY pyproject.toml README.md ./
COPY uv.lock .
RUN mkdir -p src/advanced_omi_backend
COPY src/advanced_omi_backend/__init__.py src/advanced_omi_backend/


# ============================================
# Production stage - production dependencies only
# ============================================
FROM base AS prod

# Install production dependencies only
RUN uv sync --extra deepgram

# Copy all application code
COPY . .

# Copy configuration files if they exist
COPY diarization_config.json* ./

# Copy and make startup script executable
COPY start.sh ./
RUN chmod +x start.sh

# Run the application
CMD ["./start.sh"]


# ============================================
# Dev/Test stage - includes test dependencies
# ============================================
FROM base AS dev

# Install production + test dependencies
RUN uv sync --extra deepgram --group test

# Copy all application code
COPY . .

# Copy configuration files if they exist
COPY diarization_config.json* ./

# Copy and make startup script executable
COPY start.sh ./
RUN chmod +x start.sh

# Run the application
CMD ["./start.sh"]
