#!/bin/bash
# Test Container Teardown Script
# Simplified - just uses docker compose down

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }

# Navigate to backend directory
SCRIPT_DIR="$(dirname "$0")"
cd "$SCRIPT_DIR/../backends/advanced" || exit 1

# Load .env.test if available for other environment variables
if [ -f "$SCRIPT_DIR/setup/.env.test" ]; then
    set -a
    source "$SCRIPT_DIR/setup/.env.test"
    set +a
fi

if [ "${REMOVE_VOLUMES:-false}" = "true" ]; then
    print_info "Stopping containers and removing volumes..."
    docker compose -f docker-compose-test.yml down -v
    print_success "Containers and volumes removed"
else
    print_info "Stopping containers (keeping volumes)..."
    docker compose -f docker-compose-test.yml down
    print_success "Containers stopped (volumes preserved)"
    print_warning "To remove volumes: REMOVE_VOLUMES=true ./teardown-test-containers.sh"
fi
