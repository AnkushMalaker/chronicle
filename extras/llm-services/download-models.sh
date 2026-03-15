#!/usr/bin/env bash
# Download GGUF model files for llama.cpp LLM services
#
# Usage:
#   ./download-models.sh                    # Download default models
#   ./download-models.sh --llm-only         # Download only the chat model
#   ./download-models.sh --embed-only       # Download only the embedding model
#   ./download-models.sh --custom <hf_repo> <filename>  # Download a custom GGUF

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODELS_DIR="$SCRIPT_DIR/models"
mkdir -p "$MODELS_DIR"

# Default models
DEFAULT_LLM_REPO="bartowski/zai-org_GLM-4.7-Flash-GGUF"
DEFAULT_LLM_FILE="zai-org_GLM-4.7-Flash-Q4_K_M.gguf"

DEFAULT_EMBED_REPO="nomic-ai/nomic-embed-text-v1.5-GGUF"
DEFAULT_EMBED_FILE="nomic-embed-text-v1.5.Q8_0.gguf"

download_model() {
    local repo="$1"
    local filename="$2"
    local dest="$MODELS_DIR/$filename"

    if [ -f "$dest" ]; then
        echo "✅ Already exists: $filename ($(du -h "$dest" | cut -f1))"
        return 0
    fi

    echo "📥 Downloading $filename from $repo..."

    # Use uv + huggingface_hub Python API (no global install needed, shows progress)
    uv run --with huggingface-hub python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    '$repo',
    '$filename',
    local_dir='$MODELS_DIR',
    local_dir_use_symlinks=False,
)
"

    if [ -f "$dest" ]; then
        echo "✅ Downloaded: $filename ($(du -h "$dest" | cut -f1))"
    else
        echo "❌ Download failed: $filename"
        return 1
    fi
}

case "${1:-all}" in
    --llm-only)
        download_model "$DEFAULT_LLM_REPO" "$DEFAULT_LLM_FILE"
        ;;
    --embed-only)
        download_model "$DEFAULT_EMBED_REPO" "$DEFAULT_EMBED_FILE"
        ;;
    --custom)
        if [ $# -lt 3 ]; then
            echo "Usage: $0 --custom <hf_repo> <filename>"
            exit 1
        fi
        download_model "$2" "$3"
        ;;
    all|"")
        echo "📦 Downloading default models to $MODELS_DIR"
        echo ""
        download_model "$DEFAULT_LLM_REPO" "$DEFAULT_LLM_FILE"
        echo ""
        download_model "$DEFAULT_EMBED_REPO" "$DEFAULT_EMBED_FILE"
        echo ""
        echo "🎉 All models downloaded!"
        echo ""
        echo "Set in your .env:"
        echo "  LLM_MODEL_FILE=$DEFAULT_LLM_FILE"
        echo "  EMBED_MODEL_FILE=$DEFAULT_EMBED_FILE"
        ;;
    *)
        echo "Usage: $0 [--llm-only | --embed-only | --custom <repo> <file>]"
        exit 1
        ;;
esac
