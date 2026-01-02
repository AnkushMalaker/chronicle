FROM python:3.12-bookworm

ARG PYTORCH_CUDA_VERSION

# Capture build arg as environment variable for runtime
ENV PYTORCH_CUDA_VERSION=${PYTORCH_CUDA_VERSION}

# Install system dependencies including PortAudio for pyaudio
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*
    # portaudio19-dev \
    # libasound2-dev \

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy dependency files first (for better caching)
COPY pyproject.toml ./

# Create minimal package structure for dependency installation
RUN mkdir -p src/simple_speaker_recognition
COPY src/simple_speaker_recognition/__init__.py src/simple_speaker_recognition/

# Install dependencies and package
RUN uv sync --no-dev --extra ${PYTORCH_CUDA_VERSION} && \
    uv cache clean

# Copy the full source code (after dependencies are cached)
COPY src/ src/

# Create directories
RUN mkdir -p /app/audio_chunks /app/debug /app/data /models

# Set environment variables
ENV HF_HOME=/models
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8085

# Run the service
# Use shell form to allow environment variable expansion
CMD uv run --extra ${PYTORCH_CUDA_VERSION} --no-dev simple-speaker-service 