#!/bin/bash
source "$(dirname "$0")/_engine.sh"
# tests/bin/start-containers.sh
# Start test containers with health checks

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TESTS_DIR="$SCRIPT_DIR/.."
BACKEND_DIR="$TESTS_DIR/../backend"

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

# Resolve the service profile: which backing services are real for this run.
# The resolver verifies the profile's prerequisites (API keys, reachable local
# services) and exits non-zero with the remedy if they are missing, so we never
# start a stack that is configured for a provider it cannot actually reach.
echo "🔧 Resolving test profile: ${PROFILE:-<manifest default>}"
PROFILE_EXPORTS="$(cd "$TESTS_DIR" && uv run --with-requirements test-requirements.txt \
    python scripts/resolve_profile.py ${PROFILE:+"$PROFILE"})" || exit $?
eval "$PROFILE_EXPORTS"
echo "   profile=$TEST_PROFILE config=$TEST_CONFIG_FILE"

# Start containers
echo "🐳 Starting containers..."
# shellcheck disable=SC2086  # TEST_COMPOSE_PROFILE_ARGS is intentionally word-split
$COMPOSE -f docker-compose-test.yml $TEST_COMPOSE_PROFILE_ARGS up -d

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

# Stability check - verify no containers are restart-looping
echo ""
echo "🔍 Checking container stability (waiting 5s)..."
sleep 5

RESTART_ISSUES=""
# `$COMPOSE ps -q` is docker-compatible under docker; under podman-compose it may not
# emit clean IDs, so this supplementary loop must never abort startup (the curl
# health/readiness checks above are the real gate). Tolerate query failures.
for CONTAINER_ID in $($COMPOSE -f docker-compose-test.yml ps -q 2>/dev/null || true); do
    NAME=$($ENGINE inspect --format '{{.Name}}' "$CONTAINER_ID" 2>/dev/null | sed 's/^\///')
    RESTART_COUNT=$($ENGINE inspect --format '{{.RestartCount}}' "$CONTAINER_ID" 2>/dev/null)
    case "$RESTART_COUNT" in
        ''|*[!0-9]*) continue ;;  # non-numeric / unavailable → skip
    esac
    if [ "$RESTART_COUNT" -gt 0 ]; then
        RESTART_ISSUES="${RESTART_ISSUES}   ⚠️  ${NAME} has restarted ${RESTART_COUNT} times\n"
    fi
done

if [ -n "$RESTART_ISSUES" ]; then
    echo ""
    echo "❌ Container stability check FAILED - restart loops detected:"
    echo ""
    echo -e "$RESTART_ISSUES"
    echo "   Check logs: $COMPOSE -f docker-compose-test.yml logs <service>"
    echo "   Common causes: missing env vars, import errors, dependency crashes"
    exit 1
fi
echo "✅ All containers stable (0 restarts)"

echo ""
echo "✅ Test containers are running and healthy"
echo "   Backend: http://localhost:8001"
echo "   MongoDB: localhost:${TEST_MONGODB_PORT:-27018}"
echo "   Redis: localhost:6380"
