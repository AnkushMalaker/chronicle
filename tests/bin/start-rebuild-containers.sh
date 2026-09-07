#!/bin/bash
source "$(dirname "$0")/_engine.sh"
# tests/bin/start-rebuild-containers.sh
# Stop, rebuild, and start containers (full sequence for code changes)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TESTS_DIR="$SCRIPT_DIR/.."
BACKEND_DIR="$SCRIPT_DIR/../../backend"

cd "$BACKEND_DIR"

echo "🔨 Rebuilding and starting test containers..."
echo "   This will:"
echo "   1. Stop containers"
echo "   2. Rebuild images with latest code"
echo "   3. Start containers"
echo ""

# Load environment variables from .env.test (API keys, etc.)
if [ -f "$TESTS_DIR/setup/.env.test" ]; then
    echo "📝 Loading environment variables from .env.test..."
    set -a
    source "$TESTS_DIR/setup/.env.test"
    set +a
fi

# Stop containers
echo "🛑 Stopping containers..."
$COMPOSE -f docker-compose-test.yml stop

# Rebuild and start
echo "🏗️  Rebuilding images..."
$COMPOSE -f docker-compose-test.yml up -d --build

# Flush Redis to clear stale keys from previous test runs.
# Redis uses appendonly persistence with a bind mount, so data survives
# stop/rebuild cycles. Stale conversation:current:* keys can cause test
# failures when the audio persistence job finds a Redis key pointing to
# a MongoDB document that no longer exists.
echo "🗑️  Flushing Redis for clean test state..."
$COMPOSE -f docker-compose-test.yml exec -T redis-test redis-cli FLUSHALL > /dev/null 2>&1 || true

# Wait for services
echo "⏳ Waiting for services to be ready..."
sleep 5

# Health check
MAX_RETRIES=30
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -s http://localhost:8001/health > /dev/null 2>&1; then
        echo "✅ Backend is healthy"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
        echo "❌ Backend health check failed after $MAX_RETRIES attempts"
        exit 1
    fi
    echo "   Waiting for backend... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 2
done

echo "✅ Test containers rebuilt and running"
