#!/bin/bash

# Speaker Recognition Integration Test Runner
# Mirrors the GitHub CI speaker-recognition-tests.yml workflow for local development
# Requires: .env file with HF_TOKEN and DEEPGRAM_API_KEY

set -euo pipefail  # Exit on error, undefined variables, and pipe failures

# Cleanup function for proper signal handling
cleanup_called=false
cleanup() {
    if [ "$cleanup_called" = true ]; then
        return
    fi
    cleanup_called=true

    print_info "Cleaning up on exit..."
    # Kill any background processes in this process group
    pkill -P $$ 2>/dev/null || true
    # Clean up test containers (use project name for consistency)
    COMPOSE_PROJECT_NAME="speaker-recognition-test" docker compose -f docker-compose-test.yml down -v 2>/dev/null || true
}

# Set up signal traps for proper cleanup (but not EXIT to avoid double cleanup)
trap 'cleanup; exit 130' SIGINT SIGTERM

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if we're in the right directory
if [ ! -f "pyproject.toml" ] || [ ! -d "src" ]; then
    print_error "Please run this script from the extras/speaker-recognition directory"
    exit 1
fi

print_info "Speaker Recognition Integration Test Runner"
print_info "=========================================="
print_info ".env file exists: $([ -f .env ] && echo 'yes' || echo 'no')"

# Load environment variables (CI or local)
if [ -f ".env" ] && [ -z "${HF_TOKEN:-}" ]; then
    print_info "Loading environment variables from .env..."
    set -a
    source .env
    set +a
elif [ -n "${HF_TOKEN:-}" ]; then
    print_info "Using environment variables from CI..."
    # Set up CI-specific environment variables that would normally be in .env
    export SIMILARITY_THRESHOLD=0.15
    export SPEAKER_SERVICE_HOST=speaker-service
    export COMPUTE_MODE=cpu
    export SPEAKER_SERVICE_PORT=8085
    export SPEAKER_SERVICE_URL=http://speaker-service:8085
    export SPEAKER_SERVICE_TEST_PORT=8086
    export REACT_UI_HOST=0.0.0.0
    export REACT_UI_PORT=5173
    export REACT_UI_HTTPS=false
    
    # Create .env file for the test scripts that expect it
    print_info "Creating .env file for test compatibility..."
    cat > .env << EOF
HF_TOKEN=$HF_TOKEN
DEEPGRAM_API_KEY=$DEEPGRAM_API_KEY
SIMILARITY_THRESHOLD=$SIMILARITY_THRESHOLD
SPEAKER_SERVICE_HOST=$SPEAKER_SERVICE_HOST
COMPUTE_MODE=$COMPUTE_MODE
SPEAKER_SERVICE_PORT=$SPEAKER_SERVICE_PORT
SPEAKER_SERVICE_URL=$SPEAKER_SERVICE_URL
SPEAKER_SERVICE_TEST_PORT=$SPEAKER_SERVICE_TEST_PORT
REACT_UI_HOST=$REACT_UI_HOST
REACT_UI_PORT=$REACT_UI_PORT
REACT_UI_HTTPS=$REACT_UI_HTTPS
EOF
else
    print_error "Neither .env file nor CI environment variables found!"
    print_info "For local development: create .env with HF_TOKEN and DEEPGRAM_API_KEY"
    print_info "For CI: ensure HF_TOKEN and DEEPGRAM_API_KEY secrets are set"
    exit 1
fi

# Verify required environment variables
if [ -z "$HF_TOKEN" ]; then
    print_error "HF_TOKEN not set"
    exit 1
fi

if [ -z "$DEEPGRAM_API_KEY" ]; then
    print_error "DEEPGRAM_API_KEY not set"
    exit 1
fi

print_info "HF_TOKEN length: ${#HF_TOKEN}"
print_info "DEEPGRAM_API_KEY length: ${#DEEPGRAM_API_KEY}"

# Export variables early so docker compose can use them
export HF_TOKEN
export DEEPGRAM_API_KEY
export SIMILARITY_THRESHOLD
export COMPUTE_MODE

# Install dependencies with uv
print_info "Installing dependencies with uv..."
uv sync --extra cpu --group test

print_info "Environment variables configured for testing"

# Use unique project name to avoid conflicts with development environment
export COMPOSE_PROJECT_NAME="speaker-recognition-test"

# Clean test environment
print_info "Cleaning test environment..."
# Stop any existing test containers
docker compose -f docker-compose-test.yml down -v || true

# Run speaker recognition integration tests
print_info "Running speaker recognition integration tests..."
print_info "Disabling BuildKit for integration tests (DOCKER_BUILDKIT=0)"

# Set environment variables for the test
export DOCKER_BUILDKIT=0

# Run the integration test with timeout (speaker recognition models need time)
print_info "Starting speaker recognition test (timeout: 30 minutes)..."

# Run test with proper signal forwarding and output handling
{
    timeout --foreground --kill-after=60 1800 \
        uv run pytest tests/test_speaker_service_integration.py -v -s --tb=short --log-cli-level=INFO
} || {
    exit_code=$?
    if [ $exit_code -eq 124 ]; then
        print_error "Test timed out after 30 minutes"
    elif [ $exit_code -eq 130 ]; then
        print_warning "Test interrupted by user (Ctrl+C)"
    else
        print_error "Test failed with exit code $exit_code"
    fi
    exit $exit_code
}

print_success "Speaker recognition tests completed successfully!"

# Clean up test containers
print_info "Cleaning up test containers..."
cleanup
docker system prune -f || true

print_success "Speaker Recognition integration tests completed!"