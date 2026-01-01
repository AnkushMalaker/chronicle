# GitHub Actions CI/CD Setup for Friend Lite

This sets up **automatic GitHub releases** with APK/IPA files whenever you push code.

## 🚀 How This Works

1. You push code to GitHub
2. GitHub automatically builds **both Android APK and iOS IPA**
3. **Creates GitHub Releases** with both files attached
4. You download directly from the **Releases** tab!

## 🎯 Quick Setup (2 Steps)

### Step 1: Get Expo Token
1. Go to [expo.dev](https://expo.dev) and sign in/create account
2. Go to [Access Tokens](https://expo.dev/accounts/[account]/settings/access-tokens)
3. Create a new token and copy it

### Step 2: Add GitHub Secret
1. In your GitHub repo: **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Name: `EXPO_TOKEN`
4. Value: Paste your token from Step 1
5. Click **Add secret**

## ⚡ That's It!
# GitHub Actions Workflows

## Integration Tests

### Automatic Integration Tests (`integration-tests.yml`)
- **Triggers**: Push/PR to `main` or `develop` branches affecting backend code
- **Timeout**: 15 minutes
- **Mode**: Cached mode (better for CI environment)
- **Dependencies**: Requires `DEEPGRAM_API_KEY` and `OPENAI_API_KEY` secrets

## Required Secrets

Add these secrets in your GitHub repository settings:

```
DEEPGRAM_API_KEY=your-deepgram-api-key
OPENAI_API_KEY=your-openai-api-key
```

## Test Environment

- **Runtime**: Ubuntu latest with Docker support
- **Python**: 3.12 with uv package manager
- **Services**: MongoDB (port 27018), Qdrant (ports 6335/6336), Backend (port 8001)
- **Test Data**: Isolated test directories and databases
- **Audio**: 4-minute glass blowing tutorial for end-to-end validation

## Modes

### Cached Mode (Recommended for CI)
- Reuses containers and data between test runs
- Faster startup time
- Better for containerized CI environments
- Used by default in automatic workflows

### Fresh Mode (Recommended for Local Development)
- Completely clean environment each run
- Removes all test data and containers
- Slower but more reliable for debugging
- Can be selected in manual workflow

## Troubleshooting

1. **Test Timeout**: Increase `timeout_minutes` in manual workflow
2. **Memory Issues**: Check container logs in failed run artifacts
3. **API Key Issues**: Verify secrets are set correctly in repository settings
4. **Fresh Mode Fails**: Try cached mode for comparison

## Local Testing

To run the same tests locally:

```bash
cd backends/advanced-backend

# Install dependencies
uv sync --dev

# Set up environment (copy from .env.template)
cp .env.template .env.test
# Add your API keys to .env.test

# Run Robot Framework integration tests
uv run robot --outputdir test-results --loglevel INFO tests/integration/integration_test.robot
```