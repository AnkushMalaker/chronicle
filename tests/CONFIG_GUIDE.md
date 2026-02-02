# Test Configuration Guide

## Quick Reference

Use `TEST_CONFIG_FILE` to control which config the test backend uses:

```bash
# Default: mock-services.yml (no API keys needed)
make start-rebuild

# With API keys for full integration tests
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/deepgram-openai.yml

# With invalid Deepgram key (for transcription failure tests)
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/mock-transcription-failure.yml
```

---

## Available Configs

### 1. `mock-services.yml` (Default)
**Use for:** Tests 1-4 (no API keys required)

**Features:**
- ✅ No external API calls
- ✅ Mock transcription (always succeeds)
- ✅ Mock LLM (always succeeds)
- ✅ Fast test execution
- ❌ No real transcription
- ❌ No real memory extraction

**When to use:**
- Quick local development
- PR validation (CI)
- Tests that don't need real transcription

**Tests that work:**
- Test 1: Placeholder Conversation Created Immediately
- Test 2: Normal Behavior Preserved
- Test 3: Redis Key Set Immediately
- Test 4: Multiple Sessions Create Separate Conversations

---

### 2. `deepgram-openai.yml`
**Use for:** Tests 1-6 (full integration with real APIs)

**Features:**
- ✅ Real Deepgram transcription
- ✅ Real OpenAI memory extraction
- ✅ Full end-to-end testing
- ⚠️ Requires valid API keys
- ⚠️ Costs money (minimal for tests)
- ⏱️ Slower (network calls)

**Required environment variables:**
```bash
export DEEPGRAM_API_KEY=your-valid-key
export OPENAI_API_KEY=your-valid-key
```

**When to use:**
- Testing with real transcription
- Dev/main branch validation (CI)
- Before merging features

**Tests that work:**
- All tests (Tests 1-6)

---

### 3. `mock-transcription-failure.yml`
**Use for:** Test 5 (transcription failure scenario)

**Features:**
- ✅ Triggers real Deepgram API failures (HTTP 401)
- ✅ Tests audio persistence despite failure
- ❌ Uses invalid API key (intentionally fails)
- ✅ Mock LLM (doesn't need to succeed)

**When to use:**
- Testing Test 5: "Audio Chunks Persisted Despite Transcription Failure"
- Validating always_persist behavior on failure
- Ensuring audio isn't lost when transcription fails

**Tests that work:**
- Test 5: Audio Chunks Persisted Despite Transcription Failure
- Tests 1-4 also work (don't need transcription)

---

## Usage Examples

### Running Tests 1-4 (No API Keys)

```bash
# Start containers with mock config (default)
cd tests
make start-rebuild

# Run tests
make test-quick
# OR run specific test
uv run robot --test "Placeholder Conversation Created Immediately With Always Persist" \
  --outputdir results integration/always_persist_audio_tests.robot
```

### Running All Tests (Tests 1-6) with API Keys

```bash
# Set API keys
export DEEPGRAM_API_KEY=your-key
export OPENAI_API_KEY=your-key

# Start containers with full API config
cd tests
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/deepgram-openai.yml

# Run all tests
uv run robot --outputdir results integration/always_persist_audio_tests.robot
```

### Running Test 5 (Transcription Failure)

```bash
# Start containers with failure config
cd tests
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/mock-transcription-failure.yml

# Run Test 5
uv run robot --test "Audio Chunks Persisted Despite Transcription Failure" \
  --outputdir results integration/always_persist_audio_tests.robot
```

---

## Switching Configs

To switch configs, you need to **restart containers** because the config is loaded at startup:

```bash
# Stop containers
make stop

# Start with new config
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/deepgram-openai.yml

# Or use one command
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/mock-services.yml
```

**Note:** Just changing `TEST_CONFIG_FILE` and running tests won't work - you must restart containers!

---

## Verifying Active Config

Check which config is loaded:

```bash
docker compose -f docker-compose-test.yml exec -T chronicle-backend-test \
  env | grep CONFIG_FILE
```

Expected output:
```
CONFIG_FILE=/app/test-configs/mock-services.yml
```

---

## Troubleshooting

### Issue: Tests fail with "streaming transcription not available"
**Solution:** Using `deepgram-openai.yml` but `DEEPGRAM_API_KEY` is empty
```bash
export DEEPGRAM_API_KEY=your-key
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/deepgram-openai.yml
```

### Issue: Test 5 doesn't trigger transcription failure
**Solution:** Not using `mock-transcription-failure.yml` config
```bash
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/mock-transcription-failure.yml
```

### Issue: Changed config but tests still use old config
**Solution:** Must restart containers, not just rerun tests
```bash
make start-rebuild TEST_CONFIG_FILE=/app/test-configs/mock-services.yml
```

---

## CI/CD Integration

**GitHub Actions automatically sets the right config:**

- **PR runs:** Uses `mock-services.yml` (no API keys)
- **Dev/main runs:** Uses `deepgram-openai.yml` (with secrets)
- **Label-triggered:** Uses `deepgram-openai.yml` (with secrets)

You don't need to set `TEST_CONFIG_FILE` in CI - it's handled automatically.

---

## Summary

| Config | API Keys Needed | Tests Supported | Use Case |
|--------|----------------|-----------------|----------|
| `mock-services.yml` | ❌ None | 1-4 | Quick local dev, PRs |
| `deepgram-openai.yml` | ✅ Both | 1-6 | Full integration, releases |
| `mock-transcription-failure.yml` | ⚠️ Invalid | 1-5 | Testing failure scenarios |

**Default:** `mock-services.yml` (fastest, no costs, works for most tests)
