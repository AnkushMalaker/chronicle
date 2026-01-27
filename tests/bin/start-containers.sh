#!/bin/bash
# tests/bin/start-containers.sh
# Start test containers with health checks

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TESTS_DIR="$SCRIPT_DIR/.."
BACKEND_DIR="$TESTS_DIR/../backends/advanced"

cd "$BACKEND_DIR"

echo "🚀 Starting test containers..."

# Check if .env.test exists, create from template if needed
if [ ! -f "$TESTS_DIR/setup/.env.test" ]; then
    echo "📝 Creating .env.test from template..."
    if [ -f "$TESTS_DIR/setup/.env.test.template" ]; then
        cp "$TESTS_DIR/setup/.env.test.template" "$TESTS_DIR/setup/.env.test"
    else
        echo "❌ Error: .env.test.template not found"
        exit 1
    fi
fi

# Load environment variables from .env.test (API keys, etc.)
if [ -f "$TESTS_DIR/setup/.env.test" ]; then
    echo "📝 Loading environment variables from .env.test..."
    set -a
    source "$TESTS_DIR/setup/.env.test"
    set +a
fi

# Start containers
echo "🐳 Starting Docker containers..."
docker compose -f docker-compose-test.yml up -d

# Wait for services to be healthy
echo "⏳ Waiting for services to be healthy..."
sleep 5

# Check backend health
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

# Check readiness (includes dependencies)
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -s http://localhost:8001/readiness > /dev/null 2>&1; then
        echo "✅ All services are ready"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
        echo "❌ Readiness check failed after $MAX_RETRIES attempts"
        exit 1
    fi
    echo "   Waiting for services to be ready... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 2
done

echo ""
echo "✅ Test containers are running and healthy"
echo "   Backend: http://localhost:8001"
echo "   MongoDB: localhost:27018"
echo "   Redis: localhost:6380"
echo "   Qdrant: localhost:6337/6338"
