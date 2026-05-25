#!/bin/bash
set -a && source .env 2>/dev/null; set +a
uv run python main.py "$@"
