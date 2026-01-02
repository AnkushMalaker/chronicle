# Test Configuration Files

This directory contains configuration variants for testing different provider combinations.

## Available Test Configs

### `deepgram-openai.yml` - Cloud Services
- **STT**: Deepgram Nova 3
- **LLM**: OpenAI GPT-4o-mini
- **Embedding**: OpenAI text-embedding-3-small
- **Memory**: Chronicle native
- **Use Case**: Cloud-based testing when API credits available
- **Required**: `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`

### `parakeet-ollama.yml` - Full Local Stack
- **STT**: Parakeet ASR (local)
- **LLM**: Ollama llama3.1:latest
- **Embedding**: Ollama nomic-embed-text
- **Memory**: Chronicle native
- **Use Case**: Offline testing, no API keys needed
- **Required**: Parakeet ASR running on port 8767, Ollama running

### `full-local.yml` - Alias
Symlink to `parakeet-ollama.yml` for convenience.

## Usage

### With run-test.sh

```bash
# Test with Deepgram + OpenAI (cloud)
CONFIG_FILE=../../tests/configs/deepgram-openai.yml ./backends/advanced/run-test.sh

# Test with Parakeet + Ollama (local)
CONFIG_FILE=../../tests/configs/parakeet-ollama.yml ./backends/advanced/run-test.sh

# Using the full-local alias
CONFIG_FILE=../../tests/configs/full-local.yml ./backends/advanced/run-test.sh
```

### With Docker Compose

```bash
# From backends/advanced/
CONFIG_FILE=../../tests/configs/deepgram-openai.yml docker compose -f docker-compose-test.yml up
```

### Matrix Testing

Test all configurations:

```bash
for cfg in tests/configs/*.yml; do
  echo "Testing with: $cfg"
  CONFIG_FILE=$cfg ./backends/advanced/run-test.sh || exit 1
done
```

## Creating New Test Configs

When creating a new test configuration:

1. **Name it descriptively**: `{stt}-{llm}.yml` (e.g., `mistral-openai.yml`)
2. **Use environment variables**: Always use `${VAR:-default}` pattern for secrets
3. **Set appropriate defaults**: Update the `defaults:` section to match your provider combo
4. **Include only required models**: Don't include models that aren't used
5. **Document requirements**: Update this README with required environment variables

### Example Structure

```yaml
# tests/configs/example-config.yml
defaults:
  llm: provider-llm
  embedding: provider-embed
  stt: stt-provider
  vector_store: vs-qdrant

models:
  - name: provider-llm
    model_type: llm
    model_provider: your_provider
    api_key: ${YOUR_API_KEY:-}
    # ... model config

  - name: stt-provider
    model_type: stt
    model_provider: your_stt_provider
    api_key: ${YOUR_STT_API_KEY:-}
    # ... stt config

memory:
  provider: chronicle
  # ... memory config
```

## Environment Variables

Test configs use environment variable substitution to avoid hardcoding secrets:

- **Pattern**: `${VAR_NAME:-default_value}`
- **Example**: `api_key: ${OPENAI_API_KEY:-}` (empty string if not set)
- **Example**: `model_url: ${PARAKEET_ASR_URL:-http://localhost:8767}` (fallback to default)

### Required by Config

**deepgram-openai.yml**:
- `DEEPGRAM_API_KEY` - Deepgram transcription API key
- `OPENAI_API_KEY` - OpenAI LLM and embeddings API key

**parakeet-ollama.yml**:
- `PARAKEET_ASR_URL` (optional) - Defaults to `http://localhost:8767`
- No API keys needed (all local services)

## Best Practices

1. **Never hardcode secrets**: Always use environment variables
2. **Test locally first**: Verify config works before adding to repo
3. **Document dependencies**: Update this README with service requirements
4. **Keep configs minimal**: Only include models actually used in tests
5. **Version control**: Test configs are tracked (no secrets), backups are ignored

## Adding More Combinations

As you add support for new providers, create corresponding test configs:

- `mistral-openai.yml` - Mistral Voxtral STT + OpenAI LLM
- `deepgram-ollama.yml` - Deepgram STT + Local Ollama LLM
- `parakeet-openai.yml` - Local Parakeet STT + OpenAI LLM
- etc.

Each new config should follow the naming convention and documentation pattern above.
