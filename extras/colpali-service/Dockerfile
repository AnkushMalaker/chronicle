# syntax=docker/dockerfile:1
#
# ColPali-family visual document retrieval. Self-contained (plain pip) like the
# Kokoro TTS image, so adding this service does not touch any shared uv.lock.
# torch comes from the PyTorch CUDA wheel index (default cu126).

ARG PYTORCH_CUDA_VERSION=cu126

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1
WORKDIR /app

ARG PYTORCH_CUDA_VERSION

# curl: healthcheck. libgl/libglib: Pillow + transformers image handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        --index-url "https://download.pytorch.org/whl/${PYTORCH_CUDA_VERSION}" \
        "torch>=2.7" "torchvision"

RUN pip install --no-cache-dir \
        "fastapi>=0.115.12" \
        "uvicorn>=0.34.2" \
        "python-multipart>=0.0.6" \
        "numpy>=1.26" \
        "pillow>=10.0" \
        "colpali-engine>=0.3.10" \
        "transformers>=4.50"

COPY app.py index.py ./

ENV COLPALI_MODEL="vidore/colSmol-256M"
ENV COLPALI_INDEX_DIR="/index"
ENV HF_HOME="/models"

EXPOSE 8790

CMD ["python", "app.py", "--port", "8790"]
