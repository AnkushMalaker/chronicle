#!/bin/bash
source "$(dirname "$0")/_engine.sh"
# tests/bin/stop-containers.sh
# Stop test containers (preserves volumes)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../../backend"

cd "$BACKEND_DIR"

echo "🛑 Stopping test containers..."
$COMPOSE -f docker-compose-test.yml stop

echo "✅ Test containers stopped (volumes preserved)"
echo "   Use 'make start' to restart"
echo "   Use 'make containers-clean' to remove everything (saves logs first)"
