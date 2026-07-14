#!/bin/bash
source "$(dirname "$0")/_engine.sh"
# tests/bin/status-containers.sh
# Show container health and status

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../setup/.env.test"

# Get project name (from docker-compose-test.yml)
# The project name is set in the compose file as 'backend-test'
PROJECT_NAME="backend-test"

echo "📊 Test Container Status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Show container status
$ENGINE ps -a --filter "name=$PROJECT_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check for restart loops
echo ""
echo "🔄 Restart Counts:"
HAS_RESTARTS=false
for CONTAINER_ID in $($ENGINE ps -q --filter "name=$PROJECT_NAME" 2>/dev/null); do
    NAME=$($ENGINE inspect --format '{{.Name}}' "$CONTAINER_ID" | sed 's/^\///')
    RESTART_COUNT=$($ENGINE inspect --format '{{.RestartCount}}' "$CONTAINER_ID")
    if [ "$RESTART_COUNT" -gt 0 ]; then
        echo "   ⚠️  ${NAME}: ${RESTART_COUNT} restarts"
        HAS_RESTARTS=true
    fi
done
if [ "$HAS_RESTARTS" = false ]; then
    echo "   ✅ All containers stable (0 restarts)"
fi

# Check if backend is responsive
echo ""
echo "🏥 Health Checks:"
if curl -s http://localhost:8001/health > /dev/null 2>&1; then
    echo "   ✅ Backend (http://localhost:8001/health)"
else
    echo "   ❌ Backend (http://localhost:8001/health)"
fi

if curl -s http://localhost:8001/readiness > /dev/null 2>&1; then
    echo "   ✅ Services Ready (http://localhost:8001/readiness)"
else
    echo "   ❌ Services Not Ready (http://localhost:8001/readiness)"
fi

echo ""
