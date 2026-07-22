#!/bin/bash
# Launch the Chronicle Vault Sync menu bar app (or pass a subcommand: install, logs, ...).
set -a
source ../../.env 2>/dev/null
source .env 2>/dev/null
set +a
exec uv run python main.py "$@"
