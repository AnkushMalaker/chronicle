#!/bin/bash
# tests/bin/stop-containers.sh
# Stop test containers (preserves volumes)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../../backends/advanced"

cd "$BACKEND_DIR"

echo "🛑 Stopping test containers..."
docker compose -f docker-compose-test.yml stop

echo "✅ Test containers stopped (volumes preserved)"
echo "   Use 'make start' to restart"
echo "   Use 'make containers-clean' to remove everything (saves logs first)"
