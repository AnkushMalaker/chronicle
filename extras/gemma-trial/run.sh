#!/bin/bash
# Quick Gemma 3n transcription trial
# Usage: ./run.sh <audio_file> [--prompt "custom prompt"] [--max-tokens 8192]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AUDIO="${1:-}"

if [ -z "$AUDIO" ]; then
    echo "Usage: ./run.sh <audio_file> [--prompt 'custom prompt'] [--max-tokens N]"
    exit 1
fi

# Pass all args through to the python script
shift
uv run --with "transformers>=4.53.0,torch,accelerate,soundfile,pillow,torchvision,timm" \
    python3 "$SCRIPT_DIR/transcribe.py" "$AUDIO" "$@"
