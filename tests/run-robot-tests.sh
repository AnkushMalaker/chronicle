#!/bin/bash

# Robot Framework Test Runner
# Mirrors the GitHub CI robot-tests.yml workflow for local development
# Requires: API keys in .env file or CI environment

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

print_info "Robot Framework Test Runner"
print_info "============================"

# Configuration
CLEANUP_CONTAINERS="${CLEANUP_CONTAINERS:-false}"  # Changed default: keep containers running for faster re-runs
OUTPUTDIR="${OUTPUTDIR:-results}"

# Use Deepgram + OpenAI config for full API tests
# Set TEST_CONFIG_FILE to point to deepgram-openai.yml inside the container
export TEST_CONFIG_FILE="/app/test-configs/deepgram-openai.yml"

# Load environment variables (CI or local)
if [ -f "setup/.env.test" ] && [ -z "$DEEPGRAM_API_KEY" ]; then
    print_info "Loading environment variables from setup/.env.test..."
    set -a
    source setup/.env.test
    set +a
elif [ -n "$DEEPGRAM_API_KEY" ]; then
    print_info "Using environment variables from CI..."
else
    print_warning "No .env.test file or CI environment variables found"
    print_info "For local development: Create tests/setup/.env.test with API keys"
    print_info "For CI: ensure DEEPGRAM_API_KEY and OPENAI_API_KEY secrets are set"
fi

# Verify required environment variables
if [ -z "$DEEPGRAM_API_KEY" ]; then
    print_error "DEEPGRAM_API_KEY not set"
    exit 1
fi

if [ -z "$OPENAI_API_KEY" ]; then
    print_error "OPENAI_API_KEY not set"
    exit 1
fi

print_info "DEEPGRAM_API_KEY length: ${#DEEPGRAM_API_KEY}"
print_info "OPENAI_API_KEY length: ${#OPENAI_API_KEY}"
print_info "Using config file: $CONFIG_FILE"

# Load HF_TOKEN from speaker-recognition/.env for test environment
SPEAKER_ENV="../extras/speaker-recognition/.env"
if [ -f "$SPEAKER_ENV" ] && [ -z "$HF_TOKEN" ]; then
    print_info "Loading HF_TOKEN from speaker-recognition service..."
    set -a
    source "$SPEAKER_ENV"
    set +a

    if [ -n "$HF_TOKEN" ]; then
        # Mask token for display
        if [ ${#HF_TOKEN} -gt 15 ]; then
            MASKED_TOKEN="${HF_TOKEN:0:5}***************${HF_TOKEN: -5}"
        else
            MASKED_TOKEN="***************"
        fi
        print_info "HF_TOKEN configured: $MASKED_TOKEN"
    fi
elif [ -n "$HF_TOKEN" ]; then
    # Already set (e.g., from CI)
    if [ ${#HF_TOKEN} -gt 15 ]; then
        MASKED_TOKEN="${HF_TOKEN:0:5}***************${HF_TOKEN: -5}"
    else
        MASKED_TOKEN="***************"
    fi
    print_info "HF_TOKEN configured: $MASKED_TOKEN"
else
    print_warning "HF_TOKEN not found - speaker recognition tests may fail"
    print_info "Configure via wizard: uv run --with-requirements ../setup-requirements.txt python ../wizard.py"
fi

export HF_TOKEN

# Create test environment file if it doesn't exist
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

# API Keys (from environment)
OPENAI_API_KEY=${OPENAI_API_KEY}
DEEPGRAM_API_KEY=${DEEPGRAM_API_KEY}

# Test Configuration
TEST_TIMEOUT=120
TEST_DEVICE_NAME=robot-test
EOF
    print_success "Created setup/.env.test"
fi

# Start test containers using dedicated startup script
FRESH_BUILD=true "$TESTS_DIR/setup-test-containers.sh"

# Run Robot Framework tests via Makefile
# Dependencies are handled automatically by 'uv run' in Makefile
print_info "Running Robot Framework tests..."
print_info "Output directory: $OUTPUTDIR"

# Delegate to Makefile with timeout
if timeout 30m make all OUTPUTDIR="$OUTPUTDIR"; then
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

output_file = os.getenv('OUTPUTDIR', 'results') + '/output.xml'
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
            status_elem = test.find('./status')
            if status_elem is not None and status_elem.get('status') == 'FAIL':
                test_name = test.get('name')
                msg = status_elem.text or status_elem.get('message', 'No message')
                print(f'\n  - {test_name}')
                # Print first 150 chars of error message
                if msg:
                    print(f'    {msg[:150]}...' if len(msg) > 150 else f'    {msg}')

    # Print where to find full results
    print(f'\n📄 Full results: {output_file.replace("/output.xml", "/log.html")}')
PYTHON_SCRIPT
fi

# Capture container logs before cleanup (always, for debugging)
print_info "Capturing container logs for debugging..."
LOG_DIR="${TESTS_DIR}/${OUTPUTDIR}/container-logs"
mkdir -p "$LOG_DIR"

cd "$BACKEND_DIR"

# Capture container status
print_info "Capturing container status..."
docker compose -f docker-compose-test.yml ps > "$LOG_DIR/container-status.txt" 2>&1 || true

# Capture worker registration status
print_info "Capturing worker registration status..."
docker compose -f docker-compose-test.yml exec -T workers-test uv run python -c '
from rq import Worker
from redis import Redis
import os

redis_url = os.getenv("REDIS_URL", "redis://redis-test:6379/0")
r = Redis.from_url(redis_url)
workers = Worker.all(connection=r)

print(f"Total workers: {len(workers)}")
print(f"\nWorker details:")
for i, worker in enumerate(workers, 1):
    print(f"  {i}. {worker.name}")
    print(f"     State: {worker.state}")
    print(f"     Queues: {[q.name for q in worker.queues]}")
    print(f"     Current job: {worker.get_current_job()}")
    print()
' > "$LOG_DIR/worker-status.txt" 2>&1 || echo "Failed to capture worker status" > "$LOG_DIR/worker-status.txt"

# Capture logs from all services
print_info "Capturing service logs..."
SERVICES=(chronicle-backend-test workers-test mongo-test redis-test qdrant-test speaker-service-test)
for service in "${SERVICES[@]}"; do
    docker compose -f docker-compose-test.yml logs --tail=200 "$service" > "$LOG_DIR/${service}.log" 2>&1 || true
done

# Capture container resource usage
print_info "Capturing container resource usage..."
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}" > "$LOG_DIR/container-stats.txt" 2>&1 || true

print_success "Container logs saved to: $LOG_DIR"

cd "$TESTS_DIR"

# Cleanup test containers
if [ "$CLEANUP_CONTAINERS" = "true" ]; then
    REMOVE_VOLUMES=true "$TESTS_DIR/teardown-test-containers.sh"
else
    print_warning "Keeping containers running for next test (CLEANUP_CONTAINERS=false)"
    print_info "To cleanup manually: REMOVE_VOLUMES=true ./teardown-test-containers.sh"
fi

if [ $TEST_EXIT_CODE -eq 0 ]; then
    print_success "Robot Framework tests completed successfully!"
else
    print_error "Robot Framework tests failed!"
fi

exit $TEST_EXIT_CODE
