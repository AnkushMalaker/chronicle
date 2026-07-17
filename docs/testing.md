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

The default fast lane excludes the MongoDB integration module and manual scripts under
`tests/scripts/`:

```bash
cd backends/advanced
uv sync --locked --group test
uv run --group test pytest \
  --ignore=tests/test_audio_persistence_mongodb.py \
  --cov=advanced_omi_backend \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-report=html
```

Run the MongoDB integration module explicitly when the test database is available:

```bash
uv run --group test pytest tests/test_audio_persistence_mongodb.py
```

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

The coverage baseline and test-ownership cleanup plan are recorded in the
[test coverage audit](test-coverage-audit.md).
