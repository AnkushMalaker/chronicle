#!/bin/bash
source "$(dirname "$0")/scripts/check_uv.sh"

# Run from the repository root: setup-requirements.txt lists chronicle-setup as a
# relative path, and uv resolves that against the working directory.
cd "$(dirname "$0")" || exit 1
uv run --with-requirements setup-requirements.txt python services.py stop --all
