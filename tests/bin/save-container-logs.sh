#!/bin/bash
# tests/bin/save-container-logs.sh
# CRITICAL: Always called before docker compose down -v
# Saves all container logs to timestamped directory

set -e

TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../logs/$TIMESTAMP"

mkdir -p "$LOG_DIR"

echo "📝 Saving container logs to logs/$TIMESTAMP/"

# Get project name from .env.test or use default
ENV_FILE="$SCRIPT_DIR/../setup/.env.test"
PROJECT_NAME="advanced-backend-test"  # Default

if [ -f "$ENV_FILE" ]; then
    # Try to read COMPOSE_PROJECT_NAME from .env.test
    FOUND_NAME=$(grep COMPOSE_PROJECT_NAME "$ENV_FILE" | cut -d= -f2)
    if [ -n "$FOUND_NAME" ]; then
        PROJECT_NAME="$FOUND_NAME"
    fi
fi

# Service list (based on docker-compose-test.yml)
SERVICES="chronicle-backend-test workers-test mongo-test redis-test qdrant-test speaker-service-test"

# Save logs for each service
for service in $SERVICES; do
    CONTAINER="${PROJECT_NAME}-${service}-1"
    echo "  - Saving $service logs..."
    docker logs "$CONTAINER" > "$LOG_DIR/$service.log" 2>&1 || echo "    Warning: Could not save logs for $CONTAINER"
done

# Save container status
echo "  - Saving container status..."
docker ps -a --filter "name=$PROJECT_NAME" > "$LOG_DIR/container-status.txt" 2>&1 || true

# Save container stats (resource usage)
echo "  - Saving container stats..."
docker stats --no-stream --no-trunc --filter "name=$PROJECT_NAME" > "$LOG_DIR/container-stats.txt" 2>&1 || true

# Copy test results if they exist
if [ -d "$SCRIPT_DIR/../results" ]; then
    echo "  - Copying test results..."
    cp -r "$SCRIPT_DIR/../results" "$LOG_DIR/test-results" 2>/dev/null || true
fi

echo "✅ Logs saved to: logs/$TIMESTAMP"
echo "   View with: ls -lh $LOG_DIR"
