#!/bin/bash
source "$(dirname "$0")/_engine.sh"
# tests/bin/restart-containers.sh
# Restart test containers without rebuilding

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../../backend"

cd "$BACKEND_DIR"

echo "🔄 Restarting test containers..."
$COMPOSE -f docker-compose-test.yml restart

echo "⏳ Waiting for services to be ready..."
sleep 5

# Quick health check
if curl -s http://localhost:8001/health > /dev/null 2>&1; then
    echo "✅ Test containers restarted successfully"
else
    echo "⚠️  Containers restarted but backend health check failed"
    echo "   Check logs with: make containers-logs SERVICE=chronicle-backend-test"
fi
