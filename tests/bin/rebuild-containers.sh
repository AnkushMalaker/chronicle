#!/bin/bash
# tests/bin/rebuild-containers.sh
# Rebuild test container images (does not start containers)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../../backends/advanced"

cd "$BACKEND_DIR"

echo "🔨 Rebuilding test container images..."
echo "   This will only rebuild images, not start containers."
echo "   Use 'make start' to start containers after rebuild."
echo ""

# Build images
echo "🏗️  Building images..."
docker compose -f docker-compose-test.yml build

echo "✅ Test container images rebuilt successfully"
echo "   Run 'make start' to start the containers"
