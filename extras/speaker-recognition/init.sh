#!/bin/bash

# Run from the repository root so uv can resolve ./extras/chronicle-setup out of
# setup-requirements.txt. init.py anchors its own paths to __file__.
cd "$(dirname "$0")/../.." || exit 1
uv run --with-requirements setup-requirements.txt \
    python3 extras/speaker-recognition/init.py "$@"
