# Chronicle Test Makefile
# Shortcuts for running tests and managing test containers

.PHONY: help all clean configure \
        containers-start containers-stop containers-restart containers-rebuild \
        containers-start-rebuild containers-clean containers-status containers-logs \
        start stop restart rebuild start-rebuild status logs \
        test test-quick test-slow test-sdk test-no-api test-with-api-keys test-all-with-slow-and-sdk clean-all \
        test-asr test-asr-gpu \
        results results-path results-detailed

# Default output directory
OUTPUTDIR ?= results
TEST_DIR = endpoints integration infrastructure asr
SERVICE ?= chronicle-backend-test

# Test configuration file (set this to use different configs)
# Can be overridden with CONFIG variable for convenience
# Examples:
#   make test CONFIG=deepgram-openai.yml
#   make test-quick CONFIG=mock-services.yml
#   make start CONFIG=/app/test-configs/custom.yml
ifdef CONFIG
  # If CONFIG is just a filename, prepend the path
  ifeq ($(findstring /,$(CONFIG)),)
    export TEST_CONFIG_FILE = /app/test-configs/$(CONFIG)
  else
    export TEST_CONFIG_FILE = $(CONFIG)
  endif
else
  export TEST_CONFIG_FILE ?= /app/test-configs/deepgram-openai.yml
endif

help:
	@echo "Chronicle Test Targets:"
	@echo ""
	@echo "Quick Commands:"
	@echo "  make configure     - Set up test API keys (Deepgram, OpenAI)"
	@echo "  make test          - Start containers + run tests (uses real APIs)"
	@echo "  make test-no-api   - Run tests without API keys (CI mode)"
	@echo "  make test-quick    - Run tests on existing containers"
	@echo "  make start         - Start test containers"
	@echo "  make stop          - Stop containers (keep volumes)"
	@echo "  make restart       - Restart containers (for .env changes)"
	@echo "  make rebuild       - Rebuild images only (doesn't start)"
	@echo "  make start-rebuild - Stop + rebuild + start (for code changes)"
	@echo "  make status        - Show container status"
	@echo ""
	@echo "Running Tests:"
	@echo "  make all         - Run all tests (excludes slow/sdk/requires-gpu)"
	@echo "  make endpoints   - Run only endpoint tests"
	@echo "  make integration - Run only integration tests"
	@echo "  make infra       - Run only infrastructure tests"
	@echo ""
	@echo "ASR Tests:"
	@echo "  make test-asr      - Run ASR protocol tests (no GPU required)"
	@echo "  make test-asr-gpu  - Run ASR GPU tests (requires NVIDIA GPU)"
	@echo ""
	@echo "Special Test Tags:"
	@echo "  make test-slow                    - Run ONLY slow tests (backend restarts)"
	@echo "  make test-sdk                     - Run ONLY SDK tests (unreleased)"
	@echo "  make test-no-api                  - Run tests without API keys (CI mode)"
	@echo "  make test-all-with-slow-and-sdk   - Run ALL tests including excluded"
	@echo ""
	@echo "Container Management:"
	@echo "  make containers-start         - Start test containers"
	@echo "  make containers-stop          - Stop containers (keep volumes)"
	@echo "  make containers-restart       - Restart containers (config changes)"
	@echo "  make containers-rebuild       - Rebuild images only"
	@echo "  make containers-start-rebuild - Stop + rebuild + start (code changes)"
	@echo "  make containers-clean         - Save logs + remove everything"
	@echo "  make containers-status        - Show container health"
	@echo "  make containers-logs          - View service logs (use SERVICE=name)"
	@echo ""
	@echo "View Results:"
	@echo "  make results          - Quick terminal summary (SSH-friendly)"
	@echo "  make results-path     - Print path to HTML report for viewing"
	@echo "  make results-detailed - Detailed terminal output with full errors"
	@echo ""
	@echo "Utilities:"
	@echo "  make clean       - Remove test output files"
	@echo "  make clean-all   - Clean results + containers (saves logs)"
	@echo ""
	@echo "Environment Variables:"
	@echo "  OUTPUTDIR - Output directory (default: results)"
	@echo "  SERVICE   - Service name for logs (default: chronicle-backend-test)"
	@echo "  CONFIG    - Config file to use (e.g., deepgram-openai.yml or full path)"
	@echo ""
	@echo "Config Options:"
	@echo "  deepgram-openai.yml            - Real API keys (default)"
	@echo "  mock-services.yml              - No API keys (for CI)"
	@echo "  mock-transcription-failure.yml - Test transcription failure scenarios"
	@echo ""
	@echo "Examples:"
	@echo "  make test                          # Default (uses real APIs)"
	@echo "  make test-no-api                   # CI mode (no API keys)"
	@echo "  make test CONFIG=mock-services.yml # Custom config"
	@echo "  make endpoints CONFIG=mock-services.yml  # Endpoint tests with mock"
	@echo "  make start-rebuild CONFIG=custom.yml     # Rebuild with custom config"
	@echo "  make containers-logs SERVICE=workers-test  # View worker logs"
	@echo "  make show-config                   # Show current config"

# Run all tests (excludes slow, sdk, and requires-gpu tests for faster feedback)
# Creates a persistent fixture conversation that won't be deleted between suites
all:
	@echo "Running all tests (excluding slow, sdk, and requires-gpu tests)..."
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "All Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		--exclude slow \
		--exclude sdk \
		--exclude requires-gpu \
		$(TEST_DIR)

# Run only endpoint tests
endpoints:
	@echo "Running endpoint tests..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Endpoint Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		endpoints

# Run only integration tests
integration:
	@echo "Running integration tests..."
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Integration Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		integration

# Run only infrastructure tests
infra:
	@echo "Running infrastructure tests..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Infrastructure Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		infrastructure

# Show current test configuration
show-config:
	@echo "Current Test Configuration:"
	@echo "  TEST_CONFIG_FILE = $(TEST_CONFIG_FILE)"
	@echo ""
	@echo "To change config:"
	@echo "  make test CONFIG=deepgram-openai.yml"
	@echo "  make test CONFIG=/path/to/custom.yml"

# Clean up test output files
clean:
	@echo "Cleaning test outputs..."
	rm -f output.xml log.html report.html
	rm -rf $(OUTPUTDIR)
	@echo "Clean complete!"

# Configure test API keys (interactive setup)
configure:
	@uv run --with-requirements ../setup-requirements.txt python setup/init.py

# ============================================================================
# Container Management Targets
# ============================================================================

# Start test containers
containers-start:
	@./bin/start-containers.sh

# Stop test containers (preserve volumes)
containers-stop:
	@./bin/stop-containers.sh

# Restart test containers
containers-restart:
	@./bin/restart-containers.sh

# Rebuild test containers (images only)
containers-rebuild:
	@./bin/rebuild-containers.sh

# Stop, rebuild, and start test containers
containers-start-rebuild:
	@./bin/start-rebuild-containers.sh

# Clean test containers (ALWAYS saves logs first!)
containers-clean:
	@./bin/clean-containers.sh

# Show container status
containers-status:
	@./bin/status-containers.sh

# View container logs
containers-logs:
	@./bin/logs-containers.sh $(SERVICE)

# ============================================================================
# Convenient Aliases
# ============================================================================

start: containers-start
stop: containers-stop
restart: containers-restart
rebuild: containers-rebuild
start-rebuild: containers-start-rebuild
status: containers-status
logs: containers-logs

# ============================================================================
# Combined Workflows
# ============================================================================

# Full workflow: start containers + run all tests
# If CONFIG is specified and differs from running containers, recreates them
test:
	@if docker compose -f ../backends/advanced/docker-compose-test.yml ps chronicle-backend-test 2>/dev/null | grep -q "Up"; then \
		echo "ℹ️  Containers already running"; \
		if [ "$(CONFIG)" != "" ]; then \
			echo "🔄 CONFIG specified - will recreate containers to apply new config"; \
			$(MAKE) containers-stop; \
			$(MAKE) containers-start; \
		else \
			echo "✅ Using existing containers (use CONFIG=... to switch config)"; \
		fi \
	else \
		$(MAKE) containers-start; \
	fi
	@$(MAKE) all

# Quick workflow: run tests on existing containers (ignores CONFIG changes)
test-quick: all

# Run ONLY slow tests (backend restarts, long timeouts)
test-slow:
	@echo "Running slow tests only..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Slow Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		--include slow \
		$(TEST_DIR)

# Run ONLY SDK tests (unreleased SDK features)
test-sdk:
	@echo "Running SDK tests only..."
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "SDK Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		--include sdk \
		$(TEST_DIR)

# Run ONLY tests that require API keys (Deepgram + OpenAI)
# Uses default deepgram-openai.yml config
test-with-api-keys:
	@echo "🧪 Running tests that require API keys..."
	@if [ -z "$$DEEPGRAM_API_KEY" ] || [ -z "$$OPENAI_API_KEY" ]; then \
		echo "❌ Error: DEEPGRAM_API_KEY and OPENAI_API_KEY must be set"; \
		echo "   export DEEPGRAM_API_KEY='your-key-here'"; \
		echo "   export OPENAI_API_KEY='your-key-here'"; \
		exit 1; \
	fi
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "API Key Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		--include requires-api-keys \
		$(TEST_DIR)

# Run tests without API keys (CI mode)
# Switches to mock-services.yml config and excludes requires-api-keys tests
test-no-api:
	@echo "🔄 Running tests without API keys (CI mode)..."
	@$(MAKE) containers-stop
	@TEST_CONFIG_FILE=/app/test-configs/mock-services.yml $(MAKE) containers-start
	@echo "✅ Containers running with mock-services.yml"
	@echo "🧪 Running tests (excluding requires-api-keys)..."
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "No API Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		--exclude slow \
		--exclude sdk \
		--exclude requires-api-keys \
		$(TEST_DIR)

# Run ALL tests including slow and SDK tests
test-all-with-slow-and-sdk:
	@echo "Running ALL tests including slow and SDK tests..."
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "All Tests (Including Slow and SDK)" \
		--console verbose \
		--loglevel INFO:INFO \
		$(TEST_DIR)

# ============================================================================
# ASR Tests
# ============================================================================

# Run ASR protocol tests (no GPU required, uses mock-asr service)
test-asr:
	@echo "Running ASR protocol tests (no GPU)..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "ASR Protocol Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		--exclude requires-gpu \
		asr

# Run ASR GPU tests (requires NVIDIA GPU with actual ASR service)
test-asr-gpu:
	@echo "Running ASR GPU tests (requires GPU)..."
	@echo "NOTE: Ensure GPU ASR service is running on port 8767"
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "ASR GPU Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		--include requires-gpu \
		asr

# ============================================================================
# View Test Results
# ============================================================================

# View test results in terminal (SSH-friendly)
results:
	@uv run --with-requirements test-requirements.txt python show_results.py

# Print path to HTML report for manual viewing
results-path:
	@uv run --with-requirements test-requirements.txt python show_results.py --path

# Detailed terminal output with full error messages
results-detailed:
	@uv run --with-requirements test-requirements.txt python show_results.py --detailed

# ============================================================================
# Cleanup
# ============================================================================

# Complete cleanup: test results + containers (saves logs)
clean-all: clean containers-clean
