# Chronicle Integration Tests

## Quick Start

Start containers and run tests:
```bash
cd tests
make test           # Start containers + run tests (excludes slow/sdk/GPU)
```

Validate suite tags without starting containers:

```bash
make validate-tags
```

Or step by step:
```bash
make start          # Start test containers
make all            # Run all tests (excludes slow/sdk/GPU)
make stop           # Stop containers
```

**Note**: Default test runs exclude `slow` tests (backend restarts, long timeouts) and `sdk` tests (unreleased SDK features) for faster feedback. Run these explicitly with `make test-slow` or `make test-sdk` when needed.

## Test Suites

Run specific test suites:

```bash
make endpoints          # API endpoint tests
make integration        # End-to-end workflows
make infra              # Infrastructure resilience tests
make configuration      # Container-independent configuration tests
```

### Special Test Categories

**Slow Tests** (excluded by default for faster feedback):
```bash
make test-slow    # Run ONLY slow tests (backend restarts, long timeouts)
```
- Backend restart tests (service stop/start cycles)
- Connection resilience tests
- Tests requiring >30s timeouts
- Excluded from default `make test` runs

**SDK Tests** (excluded until SDK is released):
```bash
make test-sdk     # Run ONLY SDK tests (unreleased features)
```
- SDK client library tests
- SDK authentication tests
- SDK upload/retrieval tests
- Excluded from default `make test` runs until SDK is published

**All Tests Including Excluded**:
```bash
make test-all-with-slow-and-sdk    # Run everything including slow and SDK tests
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

## Service Profiles

There is **one** test suite. Tests are never selected by whether an API key is
present. What changes between runs is a **service profile** — a declaration of
which backing services are real and which are stubbed, listed in
[`profiles.yml`](profiles.yml).

```bash
make test                                   # mock (default): no credentials, no cost
make test PROFILE=deepgram-openai           # real Deepgram STT + real OpenAI LLM
make test PROFILE=deepgram-openai-speaker   # ...and the real speaker service
make test PROFILE=parakeet-ollama           # real local ASR + local Ollama
```

Every test runs in every profile. A profile that names a real service declares
what it needs; the harness checks that before starting anything and fails with
the exact remedy, instead of running and surfacing auth errors as test failures.

To swap in a real service, add or pick a profile — you do not touch tests. A
profile can change the provider config, flip an env var (for example
`USE_MOCK_SPEAKER_CLIENT=false`), and bring up extra compose services, which is
how `deepgram-openai-speaker` exercises the real pyannote service.

### Cassettes

Stubs do not invent output. They replay **cassettes**: recorded real provider
responses, keyed by the sha256 of the audio that produced them, committed in
[`cassettes/`](cassettes/). That is what lets a content assertion like
"the transcript mentions glass" mean the same thing under stubs and under
Deepgram — so no test has to be skipped when credentials are absent.

Recording is the only step that needs real credentials, and it is manual:

```bash
cp setup/.env.test.template setup/.env.test   # add DEEPGRAM_API_KEY / OPENAI_API_KEY
make record-cassettes PROFILE=deepgram-openai
```

Cassettes are committed, so ordinary runs make no external calls and cost
nothing.

## Development Tips

**Faster iteration:**
1. Start containers once: `make start`
2. Run specific test suite: `make endpoints`
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
make endpoints

# 4. View logs if needed
make logs SERVICE=chronicle-backend-test

# 5. Repeat
```

---

**Technical Details:** Tests use Robot Framework for end-to-end validation, but you don't need to know Robot Framework to run tests. Just use the Makefile commands above.

**For Robot Framework test development guidelines**, see:
- `TESTING_GUIDELINES.md` - Comprehensive testing patterns and standards
- `tags.md` - Approved test tags and usage
