# Chronicle Test Makefile
# Shortcuts for running tests

.PHONY: help all clean

# Default output directory
OUTPUTDIR ?= results
TEST_DIR = endpoints integration infrastructure

help:
	@echo "Chronicle Test Targets:"
	@echo ""
	@echo "Running Tests:"
	@echo "  make all         - Run all tests"
	@echo "  make endpoints   - Run only endpoint tests"
	@echo "  make integration - Run only integration tests"
	@echo "  make infra       - Run only infrastructure tests"
	@echo ""
	@echo "Utilities:"
	@echo "  make clean       - Remove test output files"
	@echo ""
	@echo "Environment Variables:"
	@echo "  OUTPUTDIR        - Output directory (default: results)"
	@echo ""
	@echo "Examples:"
	@echo "  make all                     # Full test suite"
	@echo "  make endpoints               # Only endpoint tests"
	@echo "  make all OUTPUTDIR=/tmp/out  # Custom output dir"

# Run all tests
# Creates a persistent fixture conversation that won't be deleted between suites
all:
	@echo "Running all tests..."
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "All Tests" \
		--console verbose \
		$(TEST_DIR)

# Run only endpoint tests
endpoints:
	@echo "Running endpoint tests..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Endpoint Tests" \
		--console verbose \
		endpoints

# Run only integration tests
integration:
	@echo "Running integration tests..."
	CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Integration Tests" \
		--console verbose \
		integration

# Run only infrastructure tests
infra:
	@echo "Running infrastructure tests..."
	uv run --with-requirements test-requirements.txt robot --outputdir $(OUTPUTDIR) \
		--name "Infrastructure Tests" \
		--console verbose \
		infrastructure

# Clean up test output files
clean:
	@echo "Cleaning test outputs..."
	rm -f output.xml log.html report.html
	rm -rf $(OUTPUTDIR)
	@echo "Clean complete!"
