# ============================================
# Builder - install dependencies & download model
# ============================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./

# Create minimal package structure for uv sync
RUN mkdir -p src/smart_turn_service
RUN touch src/smart_turn_service/__init__.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Download ONNX model at build time
RUN uv run python -c "\
from huggingface_hub import hf_hub_download; \
hf_hub_download('pipecat-ai/smart-turn-v3', 'smart-turn-v3.2-cpu.onnx', local_dir='/app/models')"

# ============================================
# Runtime
# ============================================
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /app/.venv/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /app/models /app/models

# Copy source
COPY src/ src/

ENV PYTHONPATH=/app
ENV MODEL_DIR=/app/models

EXPOSE 8766

CMD ["python", "-m", "smart_turn_service.service"]
