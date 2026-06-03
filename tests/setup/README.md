# Test Setup & Configuration

This directory contains all setup/teardown logic, test data, and environment configuration for Robot Framework tests.

**DEFAULT MODE**: Dev mode - keeps containers running for fast iteration between test runs.

## Directory Structure

```
tests/setup/
├── README.md                  # This file
├── setup_keywords.robot       # Suite setup and environment startup
├── teardown_keywords.robot    # Suite teardown and cleanup
├── test_env.py               # Environment variables and configuration
└── test_data.py              # Test data constants and fixtures
```

## Files

### `setup_keywords.robot`
Setup keywords with dev/prod modes:
- **Suite Setup**: Main entry point (uses TEST_MODE environment variable)
- **Dev Mode Setup** (default): Reuse containers, clear data only (~5s)
- **Dev Mode Setup With Rebuild**: Rebuild containers in dev mode (~60s)
- **Prod Mode Setup**: Complete teardown and rebuild for CI/CD (~90s)
- **Clear Test Databases**: Clear MongoDB, FalkorDB, Redis, and audio files
- **Readiness Check**: Verify service availability
- **Health Check**: Verify service health

### `teardown_keywords.robot`
Teardown keywords with dev/prod modes:
- **Suite Teardown**: Main entry point (uses TEST_MODE environment variable)
- **Dev Mode Teardown** (default): Keep containers running for next test
- **Prod Mode Teardown**: Stop containers and remove volumes (CI/CD)
- **Cleanup Test User(s)**: Remove test users by email
- **Emergency Cleanup**: Force cleanup when normal teardown fails

### `test_env.py`
Environment configuration:
- API URLs and endpoints
- Admin credentials
- Test user credentials
- Service URLs (speaker recognition, etc.)
- Loads from `.env.test` file

### `test_data.py`
Test data constants:
- Sample conversations
- Sample memories
- Test audio file paths
- Expected transcript content

## Usage

### Import in Test Files

```robot
*** Settings ***
Resource    ../resources/setup_resources.robot  # Includes setup/teardown
Variables   ../setup/test_env.py
Variables   ../setup/test_data.py

Suite Setup      Suite Setup
Test Setup       Clear Test Databases
Suite Teardown   Suite Teardown
```

Or import directly:

```robot
*** Settings ***
Resource    ../setup/setup_keywords.robot
Resource    ../setup/teardown_keywords.robot
Variables   ../setup/test_env.py
Variables   ../setup/test_data.py
```

### Environment Variables

Control test behavior via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `TEST_MODE` | `dev` | `dev` (keep containers) or `prod` (CI/CD cleanup) |
| `REBUILD` | `false` | Force container rebuild (dev mode only) |

### Usage Examples

```bash
# Dev mode (default) - keep containers running
robot tests/endpoints/conversation_tests.robot

# Prod mode - for CI/CD, full cleanup
TEST_MODE=prod robot tests/

# Rebuild in dev mode (after code changes)
REBUILD=true robot tests/
```

## Setup Strategies

### Dev Mode (Default)
```bash
robot tests/
```
- **Default behavior** - no environment variable needed
- Reuses existing containers if available
- Only clears test data (MongoDB, FalkorDB, Redis, audio files)
- Keeps containers running after tests
- **Fastest** for rapid iteration (~5s, instant if containers up)
- Best for: local development, rapid testing

### Dev Mode with Rebuild
```bash
REBUILD=true robot tests/
```
- Rebuilds containers with latest code changes
- Clears test data after rebuild
- Keeps containers running after tests
- Best for: after modifying Docker images or Python code

### Prod Mode
```bash
TEST_MODE=prod robot tests/
```
- Complete teardown of containers and volumes before setup
- Fresh rebuild from scratch
- Full cleanup after tests (stops containers, removes volumes)
- Best for: CI/CD pipelines, clean slate testing

## Workflow Tips

### Local Development Workflow (Default)
```bash
# Run tests - containers stay up (dev mode is default!)
robot tests/endpoints/auth_tests.robot

# Rapid iteration - instant startup!
robot tests/endpoints/conversation_tests.robot
robot tests/endpoints/memory_tests.robot

# When done, cleanup manually if desired
docker-compose -f backends/advanced/docker-compose-test.yml down -v
```

### After Code Changes
```bash
# Rebuild and test (containers stay up after)
REBUILD=true robot tests/
```

### CI/CD Pipeline
```bash
# Prod mode for fresh environment and full cleanup
TEST_MODE=prod robot tests/
```

## Test Data Management

### Adding Test Data

Edit `test_data.py` to add new test fixtures:

```python
# Sample conversations
SAMPLE_CONVERSATIONS = [
    {
        "id": "conv_001",
        "transcript": "This is a test conversation",
        "created_at": "2025-01-15T10:00:00Z"
    }
]

# Audio files
TEST_AUDIO_FILE = "tests/test_assets/audio.wav"
TEST_DEVICE_NAME = "Robot-test-device"
```

### Updating Environment Variables

Edit `.env.test` in the tests directory:

```bash
# Admin credentials
ADMIN_EMAIL=test-admin@example.com
ADMIN_PASSWORD=test-admin-password-123

# API endpoints
BACKEND_URL=http://localhost:8001
```

## Troubleshooting

### Containers Won't Start
```bash
# Emergency cleanup
docker-compose -f backends/advanced/docker-compose-test.yml down -v
rm -rf backends/advanced/data/test_*

# Fresh start
FRESH_RUN=true robot tests/
```

### Tests Failing After Code Changes
```bash
# Rebuild containers
REBUILD=true robot tests/
```

### Need to Inspect Containers After Failure
```bash
# Leave containers running
CLEANUP_CONTAINERS=false robot tests/endpoints/failing_test.robot

# Inspect logs
docker logs advanced-chronicle-backend-test-1
docker logs advanced-mongo-test-1

# Inspect database
docker exec -it advanced-mongo-test-1 mongosh test_db

# Cleanup when done
docker-compose -f backends/advanced/docker-compose-test.yml down -v
```

### Stale Data Between Test Runs
The `Clear Test Databases` keyword should handle this, but if issues persist:

```bash
# Force fresh environment
FRESH_RUN=true robot tests/
```

## Related Documentation

- **[@CLAUDE.md](../../CLAUDE.md)**: Project overview and development guide
- **[@tests/README.md](../README.md)**: Testing overview (if exists)
- **[@backends/advanced/README.md](../../backends/advanced/README.md)**: Backend service documentation
