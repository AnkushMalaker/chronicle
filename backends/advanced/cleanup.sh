#!/bin/bash
# Wrapper script for cleanup_state.py
# Usage: ./cleanup.sh --backup --export-audio
#
# This script runs the cleanup_state.py script inside the chronicle-backend container
# to handle data ownership and permissions correctly.
#
# Examples:
#   ./cleanup.sh --dry-run              # Preview what would be deleted
#   ./cleanup.sh --backup               # Cleanup with metadata backup
#   ./cleanup.sh --backup --export-audio  # Full backup including audio
#   ./cleanup.sh --backup --force       # Skip confirmation prompts

cd "$(dirname "$0")"
docker compose exec chronicle-backend uv run python src/scripts/cleanup_state.py "$@"
