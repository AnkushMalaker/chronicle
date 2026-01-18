#!/bin/bash
# tests/bin/clean-containers.sh
# ALWAYS saves logs before removing containers

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../../backends/advanced"

echo "🧹 Cleaning test containers..."
echo ""

# CRITICAL: Save logs first!
echo "📝 Step 1/2: Saving container logs..."
"$SCRIPT_DIR/save-container-logs.sh"
echo ""

# Now safe to remove
echo "🗑️  Step 2/2: Removing containers and volumes..."
cd "$BACKEND_DIR"
docker compose -f docker-compose-test.yml down -v

echo ""
echo "✅ Cleanup complete!"
echo "   📁 Logs preserved in: tests/logs/"
echo "   🔄 Use 'make start' for fresh containers"
