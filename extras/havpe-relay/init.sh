#!/bin/bash
source "$(dirname "$0")/../../scripts/check_uv.sh"

# Run from the repository root so uv can resolve ./extras/chronicle-setup out of
# setup-requirements.txt. init.py anchors its own paths to __file__.
cd "$(dirname "$0")/../.." || exit 1
uv run --no-project --with-requirements setup-requirements.txt \
    python extras/havpe-relay/init.py "$@"
