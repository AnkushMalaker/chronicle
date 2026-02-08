#!/bin/bash

# Robot Framework Test Runner (No API Keys Required)
# Runs tests that don't require external API services (Deepgram, OpenAI)
# Excludes tests tagged with 'requires-api-keys'

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

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if we're in the right directory
if [ ! -f "Makefile" ] || [ ! -d "endpoints" ]; then
    print_error "Please run this script from the tests/ directory"
    exit 1
fi

# Set absolute paths for consistent directory references
TESTS_DIR="$(pwd)"
BACKEND_DIR="$(cd ../backends/advanced && pwd)"

print_info "Robot Framework Test Runner (No API Keys)"
print_info "=========================================="
print_info "This runner executes tests that don't require external API services"
print_info "Tests tagged with 'requires-api-keys', 'slow', and 'sdk' are excluded"

# Configuration
CLEANUP_CONTAINERS="${CLEANUP_CONTAINERS:-false}"
OUTPUTDIR="${OUTPUTDIR:-results-no-api}"

# Use mock services config (no API keys needed)
# Set TEST_CONFIG_FILE to point to mock-services.yml inside the container
export TEST_CONFIG_FILE="/app/test-configs/mock-services.yml"

print_info "Using config file: ${TEST_CONFIG_FILE}"
print_warning "Memory extraction and transcription are disabled in this mode"

# Load environment variables if available (but don't require them)
if [ -f "setup/.env.test" ]; then
    print_info "Loading environment variables from setup/.env.test..."
    set -a
    source setup/.env.test
    set +a
fi

# Create test environment file if it doesn't exist (without API keys)
if [ ! -f "setup/.env.test" ]; then
    print_info "Creating test environment file..."
    mkdir -p setup

    cat > setup/.env.test << EOF
# API URLs
API_URL=http://localhost:8001
BACKEND_URL=http://localhost:8001
FRONTEND_URL=http://localhost:3001

# Test Admin Credentials
ADMIN_EMAIL=test-admin@example.com
ADMIN_PASSWORD=test-admin-password-123

# Test Configuration
TEST_TIMEOUT=120
TEST_DEVICE_NAME=robot-test

# Note: No API keys required for this test mode
# OPENAI_API_KEY and DEEPGRAM_API_KEY are not needed
EOF
    print_success "Created setup/.env.test"
fi

# Start test containers using dedicated startup script
FRESH_BUILD=true "$TESTS_DIR/setup-test-containers.sh"

# Run Robot Framework tests via Makefile with tag exclusion
# Exclude tests that require API keys, slow tests, and SDK tests
print_info "Running Robot Framework tests (excluding requires-api-keys, slow, sdk tags)..."
print_info "Output directory: $OUTPUTDIR"

# Run tests with tag exclusion
if timeout 30m uv run --with-requirements test-requirements.txt \
    robot --exclude requires-api-keys \
    --exclude slow \
    --exclude sdk \
    --outputdir "$OUTPUTDIR" \
    --loglevel INFO \
    --consolecolors on \
    --consolemarkers on \
    .; then
    TEST_EXIT_CODE=0
else
    TEST_EXIT_CODE=$?
fi

# Show service logs if tests failed
if [ $TEST_EXIT_CODE -ne 0 ]; then
    print_info "Showing service logs..."
    cd "$BACKEND_DIR"
    echo "=== Backend Logs (last 50 lines) ==="
    docker compose -f docker-compose-test.yml logs --tail=50 chronicle-backend-test
    echo ""
    echo "=== Worker Logs (last 50 lines) ==="
    docker compose -f docker-compose-test.yml logs --tail=50 workers-test
    cd "$TESTS_DIR"
fi

# Display test results summary
if [ -f "$OUTPUTDIR/output.xml" ]; then
    print_info "Test Results Summary:"
    uv run python3 << 'PYTHON_SCRIPT'
import xml.etree.ElementTree as ET
import os

output_file = os.getenv('OUTPUTDIR', 'results-no-api') + '/output.xml'
try:
    tree = ET.parse(output_file)
    root = tree.getroot()

    # Get overall stats
    stats = root.find('.//total/stat')
    if stats is not None:
        passed = stats.get("pass", "0")
        failed = stats.get("fail", "0")
        print(f'✅ Passed: {passed}')
        print(f'❌ Failed: {failed}')
        print(f'📊 Total: {int(passed) + int(failed)}')

        # Show failed tests if any
        if int(failed) > 0:
            print('\n❌ Failed Tests:')
            failed_tests = root.findall('.//test')
            for test in failed_tests:
                status = test.find('status')
                if status is not None and status.get('status') == 'FAIL':
                    test_name = test.get('name', 'Unknown')
                    print(f'  - {test_name}')
except Exception as e:
    print(f'Error parsing results: {e}')
PYTHON_SCRIPT
fi

# Cleanup containers if requested
if [ "$CLEANUP_CONTAINERS" = "true" ]; then
    print_info "Cleaning up test containers..."
    cd "$BACKEND_DIR"
    docker compose -f docker-compose-test.yml down -v --remove-orphans
    cd "$TESTS_DIR"
    print_success "Cleanup completed"
fi

# Final status
if [ $TEST_EXIT_CODE -eq 0 ]; then
    print_success "All tests passed! ✅"
else
    print_error "Some tests failed ❌"
    exit $TEST_EXIT_CODE
fi
