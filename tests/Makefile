# Chronicle Test Makefile
# Shortcuts for running tests and managing test containers

.PHONY: help all endpoints integration infra configuration clean configure validate-tags \
        containers-start containers-stop containers-restart containers-rebuild \
        containers-start-rebuild containers-clean containers-status containers-logs \
        start stop restart rebuild start-rebuild status logs \
        test test-quick test-custom test-slow test-sdk record-cassettes test-all-with-slow-and-sdk clean-all \
        test-asr test-asr-gpu test-asr-strix-parakeet \
        results results-path results-detailed

# Default output directory
OUTPUTDIR ?= results
TEST_DIR = endpoints integration infrastructure asr configuration
SERVICE ?= chronicle-backend-test

# Service profile: which backing services are REAL for this run.
# Declared in tests/profiles.yml; see that file for the full list.
#
#   make test                          # stubs only, no credentials (default)
#   make test PROFILE=deepgram-openai  # real Deepgram + OpenAI
#
# There is no separate "with API keys" / "without API keys" test set: the suite
# is identical in every profile. Stubs replay recorded real responses (see
# tests/cassettes/), so content assertions hold either way.
export PROFILE ?= mock

# CONFIG remains as a raw escape hatch for a one-off config file that has no
# profile entry. When set it wins over PROFILE.
ifdef CONFIG
  ifeq ($(findstring /,$(CONFIG)),)
    export TEST_CONFIG_FILE = /app/test-configs/$(CONFIG)
  else
    export TEST_CONFIG_FILE = $(CONFIG)
  endif
endif

help:
	@echo "Chronicle Test Targets:"
	@echo ""
	@echo "Quick Commands:"
	@echo "  make configure     - Set up test API keys (Deepgram, OpenAI)"
	@echo "  make test          - Start containers + run all tests (PROFILE=mock by default)"
	@echo "  make test PROFILE=deepgram-openai - Run the same suite against real providers"
	@echo "  make test-quick    - Run tests on existing containers"
	@echo "  make start         - Start test containers"
	@echo "  make stop          - Stop containers (keep volumes)"
	@echo "  make restart       - Restart containers (for .env changes)"
	@echo "  make rebuild       - Rebuild images only (doesn't start)"
	@echo "  make start-rebuild - Stop + rebuild + start (for code changes)"
	@echo "  make status        - Show container status"
	@echo ""
	@echo "Running Tests:"
	@echo "  make validate-tags - Validate Robot tags without starting containers"
	@echo "  make all         - Run all tests (excludes slow/sdk/requires-gpu)"
	@echo "  make endpoints   - Run only endpoint tests"
	@echo "  make integration - Run only integration tests"
	@echo "  make infra       - Run only infrastructure tests"
	@echo "  make configuration - Run container-independent configuration tests"
	@echo ""
	@echo "ASR Tests:"
	@echo "  make test-asr      - Run ASR protocol tests (no GPU required)"
	@echo "  make test-asr-gpu  - Run ASR GPU tests (requires NVIDIA GPU)"
	@echo "  make test-asr-strix-parakeet - Run Parakeet Strix Halo ROCm integration test"
	@echo ""
	@echo "Running Specific Tests:"
	@echo "  make test-custom T=\"Test Name\"     - Run a single test by name"
	@echo "  make test-custom TAG=audio-streaming - Run tests by tag"
	@echo "  make test-custom F=integration/foo.robot - Run a specific file"
	@echo ""
	@echo "Special Test Tags:"
	@echo "  make test-slow                    - Run ONLY slow tests (backend restarts)"
	@echo "  make test-sdk                     - Run ONLY SDK tests (unreleased)"
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
	@echo "  PROFILE   - Service profile from profiles.yml (default: mock)"
	@echo "  CONFIG    - Raw config-file override (escape hatch; wins over PROFILE)"
	@echo ""
	@echo "Profiles (tests/profiles.yml) - which services are REAL:"
	@echo "  mock                     - everything stubbed; no credentials (default)"
	@echo "  deepgram-openai          - real Deepgram STT + real OpenAI LLM"
	@echo "  deepgram-openai-speaker  - the above + real speaker recognition"
	@echo "  parakeet-ollama          - real local ASR + local Ollama (no cloud keys)"
	@echo ""
	@echo "Cassettes:"
	@echo "  make record-cassettes PROFILE=deepgram-openai  - re-record stub responses"
	@echo ""
	@echo "Examples:"
	@echo "  make test                              # stubs, free, no keys"
	@echo "  make test PROFILE=deepgram-openai      # same suite, real providers"
	@echo "  make containers-logs SERVICE=workers-test  # View worker logs"
	@echo "  make show-config                       # Show current config"

# Run all tests (excludes slow, sdk, and requires-gpu tests for faster feedback)
# Creates a persistent fixture conversation that won't be deleted between suites
all: validate-tags
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
endpoints: validate-tags
	@echo "Running endpoint tests..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Endpoint Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		endpoints

# Run only integration tests
integration: validate-tags
	@echo "Running integration tests..."
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Integration Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		integration

# Run only infrastructure tests
infra: validate-tags
	@echo "Running infrastructure tests..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Infrastructure Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		infrastructure

# Run container-independent configuration tests
configuration: validate-tags
	@echo "Running configuration tests..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Configuration Tests" \
		--console verbose \
		--loglevel INFO:INFO \
		configuration

# Show current test configuration
show-config:
	@echo "Current Test Configuration:"
	@echo "  PROFILE          = $(PROFILE)"
	@echo "  TEST_CONFIG_FILE = $(TEST_CONFIG_FILE)"
	@echo ""
	@echo "To change profile:"
	@echo "  make test PROFILE=deepgram-openai"
	@echo "  make test CONFIG=/path/to/custom.yml"

# Validate suite metadata before spending time starting the composed environment.
validate-tags:
	@uv run --with-requirements test-requirements.txt python3 scripts/validate_robot_tags.py

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
# Auto-detects config mismatch and recreates containers if needed
test:
	@EXPECTED=$$(uv run --with-requirements test-requirements.txt python scripts/resolve_profile.py \
		--skip-checks 2>/dev/null | grep '^export TEST_CONFIG_FILE=' | cut -d= -f2); \
	if [ -n "$(TEST_CONFIG_FILE)" ]; then EXPECTED="$(TEST_CONFIG_FILE)"; fi; \
	ENGINE=$$(./bin/engine-name.sh); \
	CID=$$($$ENGINE ps -q --filter "name=chronicle-backend-test" 2>/dev/null | head -1); \
	RUNNING_CONFIG=$$($$ENGINE inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
		$$CID 2>/dev/null | grep '^CONFIG_FILE=' | cut -d= -f2); \
	if [ -n "$$RUNNING_CONFIG" ] && [ "$$RUNNING_CONFIG" = "$$EXPECTED" ]; then \
		echo "✅ Containers already running profile '$(PROFILE)' ($$RUNNING_CONFIG)"; \
	else \
		if [ -n "$$RUNNING_CONFIG" ]; then \
			echo "⚠️  Profile mismatch: running=$$RUNNING_CONFIG expected=$$EXPECTED"; \
			$(MAKE) containers-stop; \
		fi; \
		echo "🚀 Starting containers for profile '$(PROFILE)'..."; \
		$(MAKE) containers-start; \
	fi
	@$(MAKE) all

# Quick workflow: run tests on existing containers (ignores CONFIG changes)
test-quick: all

# Run specific test by name, tag, or file
# Usage:
#   make test-custom T="Chunk Count Increments In Redis Session"
#   make test-custom TAG=audio-streaming
#   make test-custom F=integration/audio_streaming_integration_tests.robot
test-custom:
	@./run-custom.sh $(if $(T),--test "$(T)") $(if $(TAG),--tag $(TAG)) $(if $(F),$(F))

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

# Record real provider responses as cassettes for the stubs to replay.
# This is the ONLY target that needs real credentials, and it is run by hand --
# never as part of a test run. Cassettes are committed, so a normal run costs
# nothing and needs no keys.
record-cassettes:
	@echo "🎙️  Recording provider cassettes (PROFILE=$(PROFILE))..."
	uv run --with-requirements test-requirements.txt --with requests \
		python scripts/record_cassettes.py

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

# Run Parakeet ROCm integration test for AMD Strix Halo
test-asr-strix-parakeet:
	@echo "Running Parakeet Strix Halo ROCm integration test..."
	@echo "Using extras/asr-services/docker-compose-test-strixhalo.yml"
	PARAKEET_TEST_COMPOSE_FILE=docker-compose-test-strixhalo.yml \
	PARAKEET_TEST_SERVICE=parakeet-asr-strixhalo-test \
	PARAKEET_SERVICE_URL=http://localhost:8768 \
	PARAKEET_HEALTH_TIMEOUT=420 \
	PARAKEET_REQUEST_TIMEOUT=300 \
	uv run --directory ../extras/asr-services pytest tests/test_parakeet_service.py -v -s

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
