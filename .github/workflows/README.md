# Chronicle GitHub Workflows

Documentation for CI/CD workflows and test automation.

## Test Workflows Overview

Chronicle uses **three separate test workflows** to balance fast PR feedback with comprehensive testing:

| Workflow | Trigger | Test Coverage | API Keys | Purpose |
|----------|---------|---------------|----------|---------|
| `robot-tests.yml` | All PRs | ~70% (no-API tests) | ❌ Not required | Fast PR validation |
| `full-tests-with-api.yml` | Push to dev/main | 100% (full suite) | ✅ Required | Comprehensive validation |
| `pr-tests-with-api.yml` | PR label trigger | 100% (full suite) | ✅ Required | Pre-merge API testing |

## Workflow Details

### 1. `robot-tests.yml` - PR Tests (No API Keys)

**File**: `.github/workflows/robot-tests.yml`

**Trigger**:
```yaml
on:
  pull_request:
    paths:
      - 'tests/**/*.robot'
      - 'tests/**/*.py'
      - 'backends/advanced/src/**'
```

**Characteristics**:
- **No secrets required** - Works for external contributors
- **Excludes**: Tests tagged with `requires-api-keys`
- **Config**: `tests/configs/mock-services.yml`
- **Test Script**: `./run-no-api-tests.sh`
- **Results**: `results-no-api/`
- **Time**: ~10-15 minutes
- **Coverage**: ~70% of test suite

**Benefits**:
- Fast feedback on PRs
- No API costs for every PR
- External contributors can run full CI
- Most development workflows covered

**What's Tested**:
- API endpoints (auth, CRUD, permissions)
- Infrastructure (workers, queues, health)
- Basic integration (non-transcription)

**What's Skipped**:
- Audio upload with transcription
- Memory operations requiring LLM
- Audio streaming with STT
- Full E2E pipeline tests

### 2. `full-tests-with-api.yml` - Dev/Main Tests (Full Suite)

**File**: `.github/workflows/full-tests-with-api.yml`

**Trigger**:
```yaml
on:
  push:
    branches: [dev, main]
    paths:
      - 'tests/**'
      - 'backends/advanced/src/**'
  workflow_dispatch:  # Manual trigger available
```

**Characteristics**:
- **Requires secrets**: `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `HF_TOKEN`
- **Includes**: All tests (including `requires-api-keys`)
- **Config**: `tests/configs/deepgram-openai.yml`
- **Test Script**: `./run-robot-tests.sh`
- **Results**: `results/`
- **Time**: ~20-30 minutes
- **Coverage**: 100% of test suite

**Benefits**:
- Full validation before deployment
- Catches API integration issues
- Validates real transcription and memory processing
- Comprehensive E2E coverage

**What's Tested**:
- Everything from `robot-tests.yml` PLUS:
- Audio upload with real transcription
- Memory extraction with LLM
- Audio streaming with STT
- Full E2E pipeline validation

### 3. `pr-tests-with-api.yml` - Label-Triggered PR Tests

**File**: `.github/workflows/pr-tests-with-api.yml`

**Trigger**:
```yaml
on:
  pull_request:
    types: [labeled, synchronize]
```

**Condition**:
```yaml
if: contains(github.event.pull_request.labels.*.name, 'test-with-api-keys')
```

**Characteristics**:
- **Requires**: PR labeled with `test-with-api-keys`
- **Requires secrets**: `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `HF_TOKEN`
- **Includes**: All tests (same as full-tests-with-api.yml)
- **Config**: `tests/configs/deepgram-openai.yml`
- **Time**: ~20-30 minutes
- **Re-runs**: On new commits while label present

**Benefits**:
- Test API integrations before merging
- Useful for PRs modifying transcription/LLM code
- Maintainers can trigger on trusted PRs
- Catches issues before they reach dev/main

**Use Cases**:
- PRs that modify transcription logic
- PRs that change memory extraction
- PRs that affect audio processing pipeline
- Before merging large feature branches

## Usage Guide

### For Contributors

**Normal PR Workflow**:
1. Push your branch
2. Create PR
3. `robot-tests.yml` runs automatically (~70% coverage)
4. Fix any failures
5. Merge when tests pass

**Testing API Integrations**:
1. Push your branch
2. Create PR
3. Ask maintainer to add `test-with-api-keys` label
4. `pr-tests-with-api.yml` runs (100% coverage)
5. Fix any failures
6. Merge when tests pass

### For Maintainers

**Adding the Label**:
```bash
# Via GitHub UI
1. Go to PR
2. Click "Labels" on right sidebar
3. Select "test-with-api-keys"

# Via GitHub CLI
gh pr edit <pr-number> --add-label "test-with-api-keys"
```

**When to Use Label**:
- PR modifies audio processing or transcription
- PR changes memory extraction logic
- PR affects LLM integration
- Before merging large features
- When in doubt about API changes

**Removing the Label**:
- Label is automatically retained on new commits
- Remove manually if no longer needed
- Saves API costs if changes don't affect APIs

## Test Results

### PR Comments

All workflows post results as PR comments:

```markdown
## 🎉 Robot Framework Test Results (No API Keys)

**Status**: ✅ All tests passed!

| Metric | Count |
|--------|-------|
| ✅ Passed | 76 |
| ❌ Failed | 0 |
| 📊 Total | 76 |

### 📊 View Reports
- [Test Report](https://pages-url/report.html)
- [Detailed Log](https://pages-url/log.html)
```

### GitHub Pages

Test reports are automatically deployed to GitHub Pages:
- **Live Reports**: Clickable links in PR comments
- **Persistence**: 30 days retention
- **Format**: HTML reports from Robot Framework

### Artifacts

Downloadable artifacts for deeper analysis:
- **HTML Reports**: `robot-test-reports-html-*`
- **XML Results**: `robot-test-results-xml-*`
- **Logs**: `robot-test-logs-*` (on failure only)
- **Retention**: 30 days for reports, 7 days for logs

## Required Secrets

### Repository Secrets

Must be configured in GitHub repository settings:

```bash
DEEPGRAM_API_KEY    # Required for full-tests-with-api.yml
OPENAI_API_KEY      # Required for full-tests-with-api.yml
HF_TOKEN            # Optional (speaker recognition)
```

**Setting Secrets**:
1. Go to repository Settings
2. Navigate to Secrets and variables → Actions
3. Click "New repository secret"
4. Add each secret

### Secret Validation

Workflows validate secrets before running tests:
```yaml
- name: Verify required secrets
  env:
    DEEPGRAM_API_KEY: ${{ secrets.DEEPGRAM_API_KEY }}
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  run: |
    if [ -z "$DEEPGRAM_API_KEY" ]; then
      echo "❌ ERROR: DEEPGRAM_API_KEY secret is not set"
      exit 1
    fi
```

## Cost Management

### API Cost Breakdown

**No-API Tests** (`robot-tests.yml`):
- **Cost**: $0 per run
- **Frequency**: Every PR commit
- **Monthly**: Potentially hundreds of runs
- **Savings**: Significant with external contributors

**Full Tests** (`full-tests-with-api.yml`, `pr-tests-with-api.yml`):
- **Transcription**: ~$0.10-0.30 per run (Deepgram)
- **LLM**: ~$0.05-0.15 per run (OpenAI)
- **Total**: ~$0.15-0.45 per run
- **Frequency**: dev/main pushes + labeled PRs
- **Monthly**: Typically 10-50 runs

### Cost Optimization

**Strategies**:
1. Most PRs use no-API tests (free)
2. Full tests only on protected branches
3. Label-triggered for selective full testing
4. No redundant API calls on every commit

**Before This System**:
- Every PR: ~$0.45 cost
- 100 PRs/month: ~$45

**After This System**:
- Most PRs: $0 cost
- 10 dev/main pushes: ~$4.50
- 5 labeled PRs: ~$2.25
- Total: ~$6.75/month (85% savings)

## Workflow Configuration

### Common Settings

All test workflows share:

```yaml
# Performance
timeout-minutes: 30
runs-on: ubuntu-latest

# Caching
- uses: actions/cache@v4
  with:
    path: /tmp/.buildx-cache
    key: ${{ runner.os }}-buildx-${{ hashFiles(...) }}

# Python setup
- uses: actions/setup-python@v5
  with:
    python-version: "3.12"

# UV package manager
- uses: astral-sh/setup-uv@v4
  with:
    version: "latest"
```

### Test Execution Pattern

```yaml
- name: Run tests
  env:
    CLEANUP_CONTAINERS: "false"  # Handled by workflow
    # API keys if needed
  run: |
    ./run-{no-api|robot}-tests.sh
    TEST_EXIT_CODE=$?
    echo "test_exit_code=$TEST_EXIT_CODE" >> $GITHUB_ENV
    exit 0  # Don't fail yet

- name: Fail workflow if tests failed
  if: always()
  run: |
    if [ "${{ env.test_exit_code }}" != "0" ]; then
      echo "❌ Tests failed"
      exit 1
    fi
```

**Benefits**:
- Artifacts uploaded even on test failure
- Clean container teardown guaranteed
- Clear separation of test execution and reporting

## Troubleshooting

### Workflow Not Triggering

**Problem**: Workflow doesn't run on PR
**Solutions**:
- Check file paths in workflow trigger
- Verify workflow file syntax (YAML)
- Check repository permissions
- Look for disabled workflows in Settings

### Secret Errors

**Problem**: "ERROR: DEEPGRAM_API_KEY secret is not set"
**Solutions**:
- Verify secret is set in repository settings
- Check secret name matches exactly (case-sensitive)
- Ensure workflow has access to secrets
- Fork PRs cannot access secrets (expected)

### Test Failures

**Problem**: Tests fail in CI but pass locally
**Solutions**:
- Check environment differences (.env.test)
- Verify test isolation (database cleanup)
- Look for timing issues (increase timeouts)
- Check Docker resource limits in CI

### Label Workflow Not Running

**Problem**: Added label but workflow doesn't trigger
**Solutions**:
- Verify label name is exactly `test-with-api-keys`
- Check workflow trigger includes `types: [labeled]`
- Try removing and re-adding label
- Push new commit to trigger synchronize event

## Maintenance

### Updating Workflows

**When to Update**:
- Adding new test categories
- Changing test execution scripts
- Modifying timeout values
- Updating artifact retention

**Testing Changes**:
1. Create test branch
2. Modify workflow file
3. Push to trigger workflow
4. Verify execution
5. Merge if successful

### Monitoring

**Key Metrics**:
- Test pass rate (target: >95%)
- Workflow execution time (target: <30min)
- API costs (target: <$10/month)
- Artifact storage usage

**Tools**:
- GitHub Actions dashboard
- Workflow run history
- Cost tracking (GitHub billing)
- Test result trends

## Reference Links

- **Test Suite README**: `tests/README.md`
- **Testing Guidelines**: `tests/TESTING_GUIDELINES.md`
- **Tag Documentation**: `tests/tags.md`
- **GitHub Actions Docs**: https://docs.github.com/en/actions
