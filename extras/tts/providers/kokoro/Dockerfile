# syntax=docker/dockerfile:1
#
# Kokoro-82M — lightweight (~82M param) GPU/CPU TTS with fixed preset voices.
# Self-contained (plain pip) like the KittenTTS image rather than the shared
# torch-oriented uv.lock, so adding this provider doesn't touch the frozen lock.
# torch is pulled from the PyTorch CUDA wheel index (default cu126).

ARG PYTORCH_CUDA_VERSION=cu126

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1
WORKDIR /app

ARG PYTORCH_CUDA_VERSION

# espeak-ng: misaki G2P fallback (required for English OOV words + other langs).
# libsndfile1: soundfile; curl: healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng \
        libsndfile1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# torch from the CUDA wheel index, then the (small) Kokoro stack on top.
RUN pip install --no-cache-dir \
        --index-url "https://download.pytorch.org/whl/${PYTORCH_CUDA_VERSION}" \
        "torch>=2.7"

RUN pip install --no-cache-dir \
        "fastapi>=0.115.12" \
        "uvicorn>=0.34.2" \
        "python-multipart>=0.0.6" \
        "numpy>=1.26" \
        "soundfile>=0.12" \
        "kokoro>=0.9.2"

# Application code
COPY common/ ./common/
COPY providers/kokoro/ ./providers/kokoro/

ENV TTS_MODEL="hexgrad/Kokoro-82M"
ENV TTS_VOICE="af_heart"
ENV TTS_LANG_CODE="a"

EXPOSE 8770

CMD ["python", "-m", "providers.kokoro.service", "--port", "8770"]
