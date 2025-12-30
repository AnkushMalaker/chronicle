# Chronicle Configuration

This directory contains Chronicle's centralized configuration files.

## Files

- **`config.yml`** - Main configuration file (gitignored, user-specific)
  - Contains model registry (LLM, STT, TTS, embeddings, vector store)
  - Memory provider settings
  - Service endpoints and API keys

- **`config.yml.template`** - Template for new setups
  - Use this to create your `config.yml`
  - Contains placeholders with `${ENV_VAR:-default}` patterns
  - No secrets included - safe to commit

## Setup

### First Time Setup

```bash
# Option 1: Run the interactive wizard (recommended)
uv run --with-requirements setup-requirements.txt python wizard.py

# Option 2: Manual setup
cp config/config.yml.template config/config.yml
# Edit config.yml to add your API keys and configure providers
```

### Environment Variable Substitution

The config system supports environment variable substitution using `${VAR:-default}` syntax:

```yaml
models:
  - name: openai-llm
    api_key: ${OPENAI_API_KEY:-}  # Uses env var or empty string
    model_url: ${OPENAI_BASE_URL:-https://api.openai.com/v1}  # With fallback
```

## Configuration Sections

### Defaults

Specifies which models to use by default:

```yaml
defaults:
  llm: openai-llm          # Default LLM model
  embedding: openai-embed  # Default embedding model
  stt: stt-deepgram       # Default speech-to-text
  vector_store: vs-qdrant # Default vector database
```

### Models

Array of model definitions - each model includes:
- `name`: Unique identifier
- `model_type`: llm, embedding, stt, tts, vector_store
- `model_provider`: openai, ollama, deepgram, parakeet, etc.
- `model_name`: Provider-specific model name
- `model_url`: API endpoint
- `api_key`: Authentication (use env vars!)
- `model_params`: Temperature, max_tokens, etc.

### Memory

Memory extraction and storage configuration:

```yaml
memory:
  provider: chronicle  # chronicle, openmemory_mcp, or mycelia
  timeout_seconds: 1200
  extraction:
    enabled: true
    prompt: "Custom extraction prompt..."
```

## Test Configurations

For testing different provider combinations, see `tests/configs/`:
- These configs are version-controlled
- Use with `CONFIG_FILE` environment variable
- No secrets - only env var placeholders

Example:
```bash
CONFIG_FILE=tests/configs/parakeet-ollama.yml ./backends/advanced/run-test.sh
```

## Hot Reload

The memory configuration section supports hot reload - changes are picked up without service restart. Model registry changes require service restart.

## Backups

The setup wizard automatically backs up `config.yml` before making changes:
- Backups: `config.yml.backup.YYYYMMDD_HHMMSS`
- These are gitignored automatically

## Documentation

For detailed configuration guides, see:
- `/Docs/memory-configuration-guide.md` - Memory settings
- `/backends/advanced/Docs/quickstart.md` - Setup guide
- `/CLAUDE.md` - Project overview
