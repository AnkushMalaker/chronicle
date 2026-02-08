#!/bin/bash
# Test Container Startup Script
# Smart startup - checks if already running, handles port conflicts automatically

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
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Navigate to backend directory
SCRIPT_DIR="$(dirname "$0")"
cd "$SCRIPT_DIR/../backends/advanced" || exit 1

# Load environment variables for tests
if [ -f "$SCRIPT_DIR/setup/.env.test" ]; then
    print_info "Loading test environment..."
    set -a
    source "$SCRIPT_DIR/setup/.env.test"
    set +a
fi

# Load HF_TOKEN from speaker-recognition service if available
SPEAKER_ENV="$SCRIPT_DIR/../extras/speaker-recognition/.env"
if [ -f "$SPEAKER_ENV" ] && [ -z "$HF_TOKEN" ]; then
    print_info "Loading HF_TOKEN from speaker-recognition..."
    set -a
    source "$SPEAKER_ENV"
    set +a
fi

# Configuration
FRESH_BUILD="${FRESH_BUILD:-false}"  # Set to true for clean rebuild with volume removal

# Check if containers are already running and healthy
if [ "$FRESH_BUILD" = "false" ]; then
    if curl -s http://localhost:8001/health > /dev/null 2>&1; then
        print_success "Test containers already running and healthy"
        print_info "Backend: http://localhost:8001"
        print_info "To force rebuild: FRESH_BUILD=true ./setup-test-containers.sh"
        exit 0
    fi
fi

# Clean up any existing test containers to avoid port conflicts
print_info "Cleaning up any existing test containers..."
docker compose -f docker-compose-test.yml down 2>/dev/null || true

# Remove any stale "Created" containers that might be holding ports
docker ps -a --filter "name=backend-test" --filter "status=created" --format "{{.Names}}" | xargs -r docker rm -f 2>/dev/null || true

# Fresh build - remove everything and rebuild
if [ "$FRESH_BUILD" = "true" ]; then
    print_info "Fresh build requested - removing volumes and rebuilding images..."
    docker compose -f docker-compose-test.yml down -v 2>/dev/null || true

    # Start with build flag
    print_info "Building and starting test containers..."
    docker compose -f docker-compose-test.yml up -d --build --wait

    print_success "Fresh build complete!"
else
    # Normal startup
    print_info "Starting test containers..."
    docker compose -f docker-compose-test.yml up -d --wait

    print_success "Containers started!"
fi

print_success "All services ready!"
print_info "Backend: http://localhost:8001"
print_info "MongoDB: localhost:27018"
print_info "Redis: localhost:6380"
print_info "Qdrant: localhost:6337"
