# Testing and coverage

Chronicle separates fast Python tests from the composed Robot Framework suite. Test commands should
be run from the directory shown below so each component uses its own dependency and coverage
configuration.

## Python tests

### Root setup and lifecycle tooling

From the repository root:

```bash
PYTHONPATH=backends/advanced/src uv run \
  --with-requirements setup-requirements.txt \
  --with pytest \
  --with pytest-cov \
  pytest tests/unit \
  --cov \
  --cov-config=.coveragerc \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-report=html
```

### Advanced backend

```bash
cd backends/advanced
uv sync --locked --group test
uv run --group test pytest \
  --cov=advanced_omi_backend \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-report=html
```

Twenty of these tests exercise a real Redis or MongoDB rather than a mock, because
what they assert only exists in the real thing: vault writes take a Redis lock that
[fails closed by design](../backends/advanced/src/advanced_omi_backend/services/memory/vault_lock.py),
and the audio-chunk and silence-trim tests assert on stored Mongo documents.

**Without those services they skip**, naming the command to start them — so a bare
checkout still gets a clean run (`379 passed, 20 skipped`). To run the full set,
start both on the ports the code defaults to:

```bash
podman run -d --rm --name chr-test-redis -p 6379:6379 redis:7-alpine
podman run -d --rm --name chr-test-mongo -p 27018:27017 mongo:8
```

CI runs them as job `services:`, so all 399 run there.

### ASR services

The default fast lane excludes the Parakeet container integration module:

```bash
cd extras/asr-services
uv sync --locked --group test
uv run --group test pytest \
  --ignore=tests/test_parakeet_service.py \
  --cov=common \
  --cov=providers \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-report=html
```

Run `tests/test_parakeet_service.py` explicitly on a machine prepared for its Docker and model
requirements.

Each command writes XML and HTML output to the component's `coverage-reports/` directory. Branch
coverage is enabled. No minimum percentage is enforced while the baseline suites are being made
green; CI still fails when a test fails.

## Integration and end-to-end tests

The repository-level `tests/` directory owns composed Robot Framework behavior. Its current
commands and environment setup are documented in [`tests/README.md`](../tests/README.md).
