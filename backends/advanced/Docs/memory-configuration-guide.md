# Memory Configuration Guide

This guide helps you set up and configure the memory system for the Friend Advanced Backend.

## Quick Start

1. **Copy the template configuration**:
```bash
Edit the `memory` section of `config/config.yml`.
```

2. **Edit `config/config.yml`** with your preferred settings in the `memory` section:
```yaml
memory:
  provider: "mem0"  # or "basic" for simpler setup
  
  # Provider-specific configuration
  mem0:
    model_provider: "openai"  # or "ollama" for local
    embedding_model: "text-embedding-3-small"
    llm_model: "gpt-5-mini"
```

3. **Set environment variables** in `.env`:
```bash
# For OpenAI
OPENAI_API_KEY=your-api-key

# For Ollama (local)
OLLAMA_BASE_URL=http://ollama:11434
```

## Configuration Options

### Memory Providers

#### mem0 (Recommended)
Advanced memory system with semantic search and context awareness.

**Configuration**:
```yaml
memory:
  provider: "mem0"
  mem0:
    model_provider: "openai"  # or "ollama"
    embedding_model: "text-embedding-3-small"
    llm_model: "gpt-5-mini"
    prompt_template: "custom_prompt_here"  # Optional
```

#### basic
Simple memory storage without advanced features.

**Configuration**:
```yaml
memory:
  provider: "basic"
  # No additional configuration needed
```

### Model Selection

#### OpenAI Models
- **LLM**: `gpt-5-mini`, `gpt-5-mini`, `gpt-3.5-turbo`
- **Embeddings**: `text-embedding-3-small`, `text-embedding-3-large`

#### Ollama Models (Local)
- **LLM**: `llama3`, `mistral`, `qwen2.5`
- **Embeddings**: `nomic-embed-text`, `all-minilm`

## Hot Reload

The configuration supports hot reloading - changes are applied automatically without restarting the service.

## Validation

The system validates your configuration on startup and logs any issues:
- Missing required fields
- Invalid provider names
- Incompatible model combinations

## Troubleshooting

### Common Issues

1. **"Provider not found"**: Check spelling in `provider` field
2. **"API key missing"**: Ensure environment variables are set
3. **"Model not available"**: Verify model names match provider's available models
4. **"Connection refused"**: Check Ollama is running if using local models

### Debug Mode

Enable debug logging by setting:
```bash
DEBUG=true
```

This provides detailed information about memory processing and configuration loading.

## Examples

### OpenAI Setup
```yaml
memory:
  provider: "mem0"
  mem0:
    model_provider: "openai"
    embedding_model: "text-embedding-3-small"
    llm_model: "gpt-5-mini"
```

### Local Ollama Setup
```yaml
memory:
  provider: "mem0"
  mem0:
    model_provider: "ollama"
    embedding_model: "nomic-embed-text"
    llm_model: "llama3"
```

### Minimal Setup
```yaml
memory:
  provider: "basic"
```

## Next Steps

- Configure action items detection in `config/config.yml` (memory.extraction)
- Set up custom prompt templates for your use case
- Monitor memory processing in the debug dashboard
