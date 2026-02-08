# Robot Framework Test Debugging Guide

Quick reference for debugging test failures efficiently.

## Quick Failure Analysis

### 1. Get Test Summary (Fastest)
```bash
# After test run, get failure summary
grep -E "tests?, [0-9]+ passed, [0-9]+ failed" results/output.xml.txt | tail -5

# Or from test output
grep -E "\| FAIL \|" results/log.html | wc -l
```

### 2. List All Failing Tests
```bash
# Get test names that failed
grep -B2 "| FAIL |" <test-output-file> | grep -E "^[A-Z].*Test ::"

# Example output:
# Get conversation permission Test :: Test that users can only acces...
# Save Diarization Settings Test :: Test saving diarization settings...
```

### 3. Get Failure Details
```bash
# Get failure reasons for each failed test
grep -A3 "| FAIL |" <test-output-file> | grep -v "^--$"

# Example output:
# | FAIL |
# POST /api/diarization-settings returned 500 (expected 200).
# Response: Internal Server Error
```

## Backend Error Correlation

### Check Backend Logs During Test Run
```bash
# Real-time backend errors
docker logs -f advanced-backend-test-chronicle-backend-test-1 2>&1 | grep -E "(ERROR|Exception|Traceback)"

# Recent errors from completed test run
docker logs advanced-backend-test-chronicle-backend-test-1 --since=1h 2>&1 | grep -B5 -A10 "Traceback"
```

### Find Errors for Specific Endpoint
```bash
# Example: Find errors related to /api/conversations
docker logs advanced-backend-test-chronicle-backend-test-1 2>&1 | grep -B10 -A10 "api/conversations"
```

## Common Failure Patterns

### 1. Function Name Mismatch (500 Error)
**Symptom**: `POST /api/endpoint returned 500 (expected 200)`

**Diagnosis**:
```bash
# Check if controller function exists
grep "async def function_name" backends/advanced/src/advanced_omi_backend/controllers/*.py

# Check route calls the right function
grep "controller\\.function_name" backends/advanced/src/advanced_omi_backend/routers/**/*.py
```

**Fix**: Update route to call correct controller function name

### 2. Connection Reset (Backend Crash)
**Symptom**: `ConnectionResetError(104, 'Connection reset by peer')`

**Diagnosis**:
```bash
# Check if backend container is running
docker ps | grep chronicle-backend-test

# Check backend crash logs
docker logs advanced-backend-test-chronicle-backend-test-1 --tail=100
```

**Fix**: Backend likely crashed - check for unhandled exceptions, restart container

### 3. Test Expects 400/422 but Gets 500
**Symptom**: `'500 in [400, 422]' should be true`

**Diagnosis**: Backend is crashing instead of returning validation error

**Common Causes**:
- Missing function (AttributeError)
- Unhandled exception in validation logic
- Missing import

### 4. Job Timeout (Deferred Status)
**Symptom**: `Memory job did not complete within 120s (last status: deferred)`

**Diagnosis**:
```bash
# Check worker logs
docker logs advanced-backend-test-workers-test-1 --tail=100

# Check Redis queue status
redis-cli -p 6380 LLEN rq:queue:default
```

**Common Causes**:
- Workers not running
- Job dependency not completed
- API key missing for external services

## Debugging Workflow

### Step 1: Quick Scan (30 seconds)
```bash
cd tests

# Count failures
grep "| FAIL |" results/output.xml | wc -l

# List failed test names
grep -B2 "| FAIL |" <output-file> | grep "Test ::"
```

### Step 2: Get Error Messages (1 minute)
```bash
# Get all failure details
grep -A3 "| FAIL |" <output-file> | less

# Or save to file for analysis
grep -B5 -A10 "| FAIL |" <output-file> > failures.txt
```

### Step 3: Check Backend Logs (2 minutes)
```bash
# Check for backend exceptions
docker logs advanced-backend-test-chronicle-backend-test-1 --since=10m 2>&1 | grep -E "(ERROR|Exception|Traceback)" | tail -50

# For specific endpoint, grep for endpoint path
docker logs advanced-backend-test-chronicle-backend-test-1 2>&1 | grep -B10 "/api/endpoint-path"
```

### Step 4: Fix and Verify (iterative)
```bash
# After code fix, rebuild backend
cd backends/advanced
docker compose -f docker-compose-test.yml build chronicle-backend-test
docker restart advanced-backend-test-chronicle-backend-test-1

# Wait for healthy status
docker ps | grep chronicle-backend-test

# Re-run failed test suite only
cd tests
uv run --with-requirements test-requirements.txt robot --outputdir results-rerun --loglevel INFO:INFO endpoints/system_admin_tests.robot
```

## Useful Test Commands

### Run Specific Test Suite
```bash
# Single suite
robot endpoints/conversation_tests.robot

# Specific test by name
robot -t "Get conversation permission Test" endpoints/conversation_tests.robot

# By tag
robot --include permissions endpoints/
```

### Run with More Debug Info
```bash
# Increase log level for debugging
robot --loglevel DEBUG:DEBUG endpoints/

# Keep only failed tests in log
robot --loglevel TRACE:INFO endpoints/
```

### Faster Iteration
```bash
# Skip test environment setup (if containers already running)
TEST_MODE=dev robot endpoints/test_file.robot

# Run without cleanup (keep containers running)
CLEANUP_CONTAINERS=false ./run-robot-tests.sh
```

## Prevention: Catch Issues Before Running Tests

### 1. Type Checking (Recommended)
```bash
cd backends/advanced
uv run mypy src/
```

### 2. Import Validation
```bash
# Quick check if all imports resolve
uv run python -c "from advanced_omi_backend.routers.modules import system_routes"
```

### 3. Lint Check
```bash
uv run ruff check src/
```

## Quick Reference: Test Container Names

| Service | Container Name | Logs Command |
|---------|---------------|--------------|
| Backend | `advanced-backend-test-chronicle-backend-test-1` | `docker logs <name>` |
| Workers | `advanced-backend-test-workers-test-1` | `docker logs <name>` |
| MongoDB | `advanced-backend-test-mongo-test-1` | `docker logs <name>` |
| Redis | `advanced-backend-test-redis-test-1` | `docker logs <name>` |
| Qdrant | `advanced-backend-test-qdrant-test-1` | `docker logs <name>` |

## Tips for Faster Debugging

1. **Keep test containers running** between iterations (`CLEANUP_CONTAINERS=false`)
2. **Run specific suites** instead of full test run
3. **Check backend health first** before running tests
4. **Use grep liberally** - don't read full logs
5. **Pattern match errors** - most failures follow common patterns
6. **Fix one failure type at a time** - similar failures often have same root cause

## Common Error Messages and Solutions

| Error Message | Likely Cause | Quick Fix |
|---------------|--------------|-----------|
| `500 Internal Server Error` | Backend exception | Check backend logs for traceback |
| `ConnectionResetError` | Backend crashed | Restart backend, check logs |
| `expected 200, got 500` | Unhandled exception | Check controller function exists |
| `Job did not complete` | Worker issue or timeout | Check worker logs, increase timeout |
| `404 Not Found` | Endpoint doesn't exist | Check route registration |
| `401 Unauthorized` | Auth token issue | Check session creation |
| `403 Forbidden` | Permission issue | Check user role in test |

---

**Last Updated**: 2026-01-15
**Maintainer**: Update this guide when you discover new debugging patterns
