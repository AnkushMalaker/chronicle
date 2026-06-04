#!/bin/bash
# Stage a nanowakeword wheel from the local source into vendor/ for the Docker
# build. PyPI tops out at 2.1.3; the model was trained/exported with the local
# 2.1.4 checkout, so we install that exact source (interpreter core only — no
# torch/train deps at runtime). Run this BEFORE `docker compose build`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../untracked/nanowakeword"
VENDOR="$HERE/vendor"

[ -d "$SRC" ] || { echo "nanowakeword source not found at $SRC"; exit 1; }

mkdir -p "$VENDOR"
rm -f "$VENDOR"/nanowakeword-*.whl
echo "Building nanowakeword wheel from $SRC ..."
uv build --wheel --project "$SRC" --out-dir "$VENDOR"
echo "Staged:"
ls -1 "$VENDOR"/nanowakeword-*.whl
echo "Now run: docker compose build"
