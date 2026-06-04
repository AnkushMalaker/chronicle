# syntax=docker/dockerfile:1
#
# KittenTTS — ultra-light CPU TTS. No GPU, no torch, so this image is fully
# self-contained (plain pip) rather than using the shared torch-oriented uv.lock.

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# KittenTTS 0.8.x bundles the espeak shared lib + data via espeakng_loader, so
# no system espeak-ng is needed. libsndfile1: soundfile; ffmpeg: optional audio
# conversion; curl: healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "fastapi>=0.115.12" \
        "uvicorn>=0.34.2" \
        "python-multipart>=0.0.6" \
        "numpy>=1.26" \
        "soundfile>=0.12" \
        "https://github.com/KittenML/KittenTTS/releases/download/0.8.1/kittentts-0.8.1-py3-none-any.whl"

# Application code
COPY common/ ./common/
COPY providers/kittentts/ ./providers/kittentts/

ENV TTS_MODEL="KittenML/kitten-tts-nano-0.8-int8"
ENV TTS_VOICE="Jasper"

EXPOSE 8770

CMD ["python", "-m", "providers.kittentts.service", "--port", "8770"]
