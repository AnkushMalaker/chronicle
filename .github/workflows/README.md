# Chronicle GitHub Workflows

Documentation for CI/CD workflows and test automation.

For the end-to-end version release checklist, including TestFlight, GitHub
release publication, container images, and post-release verification, see
[`docs/releasing.md`](../../docs/releasing.md).

## Test Workflows Overview

Chronicle uses **three separate test workflows** to balance fast PR feedback with comprehensive testing:

| Workflow | Trigger | Test selection | API Keys | Purpose |
|----------|---------|---------------|----------|---------|
| `python-tests.yml` | Relevant PRs, dev/main | Root, backend, and ASR pytest lanes | Not required | Unit tests and branch coverage reports |
| `robot-tests.yml` | All PRs | No-API Robot subset | Not required | Fast PR validation |

## Workflow Details

### 1. `robot-tests.yml` - Robot Framework Tests

**File**: `.github/workflows/robot-tests.yml`

One workflow, one test suite. What varies per run is the **service profile**
(`tests/profiles.yml`) -- which backing services are real:

| Trigger | Profiles | Secrets |
|---------|----------|---------|
| `pull_request` | `mock` | none needed |
| `push` to `dev`/`main` | `mock`, `deepgram-openai` | Deepgram + OpenAI |
| `workflow_dispatch` | chosen from a dropdown | as the profile requires |

**There is no API-key test split.** Every test runs in every profile. The `mock`
profile replays recorded real provider responses from `tests/cassettes/`, so
assertions about transcript content hold identically with or without
credentials.

Two consequences worth knowing:

- Fork pull requests work unchanged, because the PR profile needs no secrets.
- The old `test-with-api-keys` label is gone. It could never have worked for the
  fork case it was added for: a `pull_request` run from a fork gets no secrets
  regardless of labels. Supporting that would require `pull_request_target`,
  which executes untrusted code with secrets in scope.

To validate against real providers before merging, either push to `dev`, or run
the workflow manually:

```bash
gh workflow run robot-tests.yml --ref <branch> -f profile=deepgram-openai
```

## Usage Guide

### For Contributors

Run the same thing CI runs on your PR:

```bash
cd tests
make test                    # profile: mock -- free, no credentials
```

If it passes locally it should pass on the PR: same suite, same profile.

### For Maintainers

```bash
# Same suite against real providers
cd tests && make test PROFILE=deepgram-openai

# Refresh recorded provider responses (the only step needing credentials)
cd tests && make record-cassettes PROFILE=deepgram-openai
```

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
DEEPGRAM_API_KEY    # Required for the deepgram-openai profile (dev/main pushes)
OPENAI_API_KEY      # Required for the deepgram-openai profile (dev/main pushes)
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

**Real-provider runs** (`deepgram-openai` profile, dev/main pushes):
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
    make test-no-api  # or ./run-robot-tests.sh for full suite
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

### Want a real-provider run on a PR

There is no label for this, and there cannot usefully be one: a `pull_request`
run from a fork cannot read repository secrets. Instead:

```bash
gh workflow run robot-tests.yml --ref <branch> -f profile=deepgram-openai
```

Or merge to `dev`, where the `deepgram-openai` profile runs automatically.

### A profile fails immediately with a credential error

That is the harness refusing to start a stack it cannot configure. The message
names the missing variable and how to set it. Run `make test PROFILE=mock` if
you do not need real providers.

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
