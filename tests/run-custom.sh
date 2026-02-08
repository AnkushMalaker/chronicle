#!/bin/bash
# Custom test runner for debugging - runs specific test files or tags
# Usage:
#   ./run-custom.sh integration/phase1_phase2_tests.robot  # Run specific file
#   ./run-custom.sh --tag audio-streaming                  # Run by tag
#   ./run-custom.sh --test "Generic Transcription Provider Works"  # Run specific test

set -e

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

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if we're in the right directory
if [ ! -f "Makefile" ] || [ ! -d "endpoints" ]; then
    print_error "Please run this script from the tests/ directory"
    exit 1
fi

# Parse arguments
TEST_FILE=""
TAG=""
TEST_NAME=""
OUTPUTDIR="${OUTPUTDIR:-results-custom}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --tag)
            TAG="$2"
            shift 2
            ;;
        --test)
            TEST_NAME="$2"
            shift 2
            ;;
        --output)
            OUTPUTDIR="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS] [TEST_FILE]"
            echo ""
            echo "Options:"
            echo "  --tag TAG          Run tests with specific tag"
            echo "  --test NAME        Run specific test by name"
            echo "  --output DIR       Output directory (default: results-custom)"
            echo "  -h, --help         Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0 integration/phase1_phase2_tests.robot"
            echo "  $0 --tag audio-streaming"
            echo "  $0 --test \"Generic Transcription Provider Works\""
            exit 0
            ;;
        *)
            TEST_FILE="$1"
            shift
            ;;
    esac
done

# Load environment variables
if [ -f "setup/.env.test" ]; then
    print_info "Loading environment from setup/.env.test..."
    set -a
    source setup/.env.test
    set +a
else
    print_error "setup/.env.test not found. Run ./run-robot-tests.sh first to create test environment."
    exit 1
fi

# Verify services are running
print_info "Checking if test services are running..."
if ! curl -s http://localhost:8001/health > /dev/null 2>&1; then
    print_error "Backend test service is not running on port 8001"
    print_info "Start services with: ./setup-test-containers.sh"
    print_info "Or let this script start them (will take time)..."
    read -p "Start test services now? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_info "Starting test infrastructure..."
        # Get the script directory to find setup script
        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        "$SCRIPT_DIR/setup-test-containers.sh"
    else
        exit 1
    fi
fi

print_success "Backend is ready"

# Build robot command
ROBOT_CMD="uv run --with-requirements test-requirements.txt robot"
ROBOT_CMD="$ROBOT_CMD --outputdir $OUTPUTDIR"
ROBOT_CMD="$ROBOT_CMD --loglevel DEBUG"  # Enable debug logging

if [ -n "$TAG" ]; then
    print_info "Running tests with tag: $TAG"
    ROBOT_CMD="$ROBOT_CMD --include $TAG"
fi

if [ -n "$TEST_NAME" ]; then
    print_info "Running test: $TEST_NAME"
    ROBOT_CMD="$ROBOT_CMD --test \"$TEST_NAME\""
fi

if [ -n "$TEST_FILE" ]; then
    print_info "Running test file: $TEST_FILE"
    ROBOT_CMD="$ROBOT_CMD $TEST_FILE"
else
    # Run all tests if no specific file/tag/test specified
    print_info "Running all tests (no filter specified)"
    ROBOT_CMD="$ROBOT_CMD endpoints integration infrastructure"
fi

print_info "Command: $ROBOT_CMD"
print_info "Output directory: $OUTPUTDIR"
echo ""

# Run the tests
if eval $ROBOT_CMD; then
    print_success "Tests completed successfully!"
    exit 0
else
    print_error "Tests failed!"
    print_info "View results: $OUTPUTDIR/log.html"
    exit 1
fi
