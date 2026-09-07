#!/bin/bash
# Chronicle Cleanup & Backup Tool
#
# This script runs cleanup_state.py inside the chronicle-backend container.
#
# Usage:
#   ./cleanup.sh --dry-run                     Preview what would happen
#   ./cleanup.sh --backup-only                 Back up everything (no cleanup)
#   ./cleanup.sh --backup-only --export-audio  Back up with audio WAV files
#   ./cleanup.sh --backup                      Back up then clean
#   ./cleanup.sh --backup --export-audio       Back up with audio then clean
#   ./cleanup.sh --backup --force              Skip confirmation prompt

cd "$(dirname "$0")"
docker compose exec chronicle-backend python src/scripts/cleanup_state.py "$@"
