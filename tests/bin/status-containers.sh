#!/bin/bash
# tests/bin/status-containers.sh
# Show container health and status

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../setup/.env.test"

# Get project name
if [ -f "$ENV_FILE" ]; then
    PROJECT_NAME=$(grep COMPOSE_PROJECT_NAME "$ENV_FILE" | cut -d= -f2 || echo "advanced-backend-test")
else
    PROJECT_NAME="advanced-backend-test"
fi

echo "📊 Test Container Status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Show container status
docker ps -a --filter "name=$PROJECT_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

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
