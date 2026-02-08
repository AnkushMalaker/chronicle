#!/bin/bash
# tests/bin/logs-containers.sh
# View logs for specific service

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../../backends/advanced"
SERVICE=$1

if [ -z "$SERVICE" ]; then
    echo "📋 Available services:"
    echo "   - chronicle-backend-test"
    echo "   - workers-test"
    echo "   - mongo-test"
    echo "   - redis-test"
    echo "   - qdrant-test"
    echo "   - speaker-service-test"
    echo ""
    echo "Usage: make containers-logs SERVICE=<service-name>"
    echo "Example: make containers-logs SERVICE=chronicle-backend-test"
    exit 1
fi

cd "$BACKEND_DIR"

echo "📜 Viewing logs for: $SERVICE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

docker compose -f docker-compose-test.yml logs --tail=100 -f "$SERVICE"
