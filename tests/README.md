# Chronicle Integration Tests

## Quick Start

Start containers and run tests:
```bash
cd tests
make test           # Start containers + run all tests
```

Or step by step:
```bash
make start          # Start test containers
make test-all       # Run all tests
make stop           # Stop containers
```

## Test Suites

Run specific test suites:

```bash
make test-endpoints     # API endpoint tests (~40 tests, fast)
make test-integration   # End-to-end workflows (~15 tests, slower)
make test-infra         # Infrastructure resilience tests (~5 tests)
```

## Container Management

All container operations are available through simple Makefile targets:

| Command | What it does |
|---------|--------------|
| `make start` | Start test containers (or reuse if healthy) |
| `make stop` | Stop containers (saves logs automatically) |
| `make restart` | Restart containers (keep same images) |
| `make rebuild` | Rebuild images and restart (for code changes) |
| `make containers-clean` | **Saves logs** → stops → removes everything |
| `make status` | Show container health and ports |
| `make logs SERVICE=<name>` | View logs for specific service |

**Important:** Containers are NEVER removed without saving logs first!

Logs are automatically saved to: `tests/logs/YYYY-MM-DD_HH-MM-SS/`

### Available Services for Logs

```bash
make logs SERVICE=chronicle-backend-test   # Main backend service
make logs SERVICE=workers-test              # RQ workers
make logs SERVICE=mongo-test                # MongoDB
make logs SERVICE=redis-test                # Redis
make logs SERVICE=qdrant-test               # Vector database
make logs SERVICE=speaker-service-test      # Speaker recognition
```

## Test Workflows

### Full Test Run (Clean Slate)
```bash
make containers-clean   # Clean previous state (saves logs)
make test               # Start fresh + run all tests
```

### Quick Iteration (Reuse Containers)
```bash
make start              # Start containers once
make test-quick         # Run tests (fast, no container startup)
make test-quick         # Run again (even faster)
```

### Code Changes (Rebuild Required)
```bash
# After modifying Python code
make rebuild            # Rebuild images with latest code
make test-quick         # Run tests on new build
```

## Test Environment

Test services run on separate ports from production to avoid conflicts:

| Service | Test Port | Production Port |
|---------|-----------|-----------------|
| Backend API | `8001` | `8000` |
| MongoDB | `27018` | `27017` |
| Redis | `6380` | `6379` |
| Qdrant HTTP | `6337` | `6333` |
| Qdrant gRPC | `6338` | `6334` |

**Test Database:** Uses `test_db` database (isolated from production)

**Test Credentials:**
- Admin Email: `test-admin@example.com`
- Admin Password: `test-admin-password-123`
- JWT Secret: `test-jwt-signing-key-for-integration-tests`

## Troubleshooting

### Port Conflicts
```bash
make status         # See what's running
make stop           # Stop test containers
```

If ports are still in use by other services:
```bash
lsof -i :8001       # Find what's using port 8001
# Kill the process or stop the conflicting service
```

### Test Failures
```bash
# View backend logs
make logs SERVICE=chronicle-backend-test

# View worker logs
make logs SERVICE=workers-test

# Check container health
make status
```

### Clean Slate
```bash
make containers-clean    # Saves logs + full cleanup
make start               # Fresh start
```

### Container Issues

**Containers won't start:**
```bash
make status                  # Check current state
make containers-clean        # Full cleanup (saves logs)
make start                   # Start fresh
```

**Health checks failing:**
```bash
make logs SERVICE=chronicle-backend-test   # Check backend logs
# Common issues: MongoDB not ready, Redis connection failed
```

**Tests hang or timeout:**
```bash
# Check if services are healthy
make status

# View logs for stuck service
make logs SERVICE=workers-test
```

## Log Preservation

**All cleanup operations preserve logs automatically!**

When you run `make containers-clean` or `make clean-all`:

1. **Step 1:** Logs are saved to `tests/logs/YYYY-MM-DD_HH-MM-SS/`
2. **Step 2:** Containers are stopped and removed
3. **Step 3:** Volumes are removed

Each log directory contains:
- Service logs for all containers
- Container status snapshot
- Container resource usage stats
- Test results (if available)

**View saved logs:**
```bash
ls -lh tests/logs/                          # List all log archives
cat tests/logs/2026-01-17_14-30-45/chronicle-backend-test.log
```

## API Key Separation

Chronicle tests are separated into two execution paths:

### 1. No API Keys Required (~70% of tests)
These tests run without external API dependencies:
- Endpoint tests (CRUD operations, permissions)
- Infrastructure tests (workers, queues, health checks)
- Basic integration tests

**Configuration:** Uses `configs/mock-services.yml` (no transcription/LLM)

### 2. API Keys Required (~30% of tests)
These tests require external services:
- Full E2E tests with transcription (Deepgram)
- Memory extraction tests (OpenAI)
- Transcript quality verification

**Configuration:** Uses `configs/deepgram-openai.yml`

**Setup:**
```bash
# Copy template
cp setup/.env.test.template setup/.env.test

# Add API keys
DEEPGRAM_API_KEY=your-key-here
OPENAI_API_KEY=your-key-here
```

## Development Tips

**Faster iteration:**
1. Start containers once: `make start`
2. Run specific test suite: `make test-endpoints`
3. Keep containers running between test runs
4. Only rebuild when code changes: `make rebuild`

**Debugging specific tests:**
```bash
# Run Robot Framework directly for a single test file
cd tests
uv run --with-requirements test-requirements.txt robot \
    --outputdir results \
    --test "Specific Test Name" \
    endpoints/test_user.robot
```

**Clean iteration cycle:**
```bash
# 1. Make code changes
# 2. Rebuild containers
make rebuild

# 3. Run specific test suite
make test-endpoints

# 4. View logs if needed
make logs SERVICE=chronicle-backend-test

# 5. Repeat
```

---

**Technical Details:** Tests use Robot Framework for end-to-end validation, but you don't need to know Robot Framework to run tests. Just use the Makefile commands above.

**For Robot Framework test development guidelines**, see:
- `TESTING_GUIDELINES.md` - Comprehensive testing patterns and standards
- `tags.md` - Approved test tags and usage
