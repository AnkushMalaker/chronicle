# Chronicle Advanced Backend

A FastAPI backend with pluggable memory providers, real-time audio processing, and comprehensive conversation management.

[QuickStart](../../quickstart.md) | [Memory Providers](./MEMORY_PROVIDERS.md) | [Configuration Guide](./Docs/memories.md)

## Key Features

### Memory System
- **Pluggable Memory Providers**: Choose between Chronicle native or OpenMemory MCP
- **Enhanced Memory Extraction**: Individual facts instead of generic transcripts
- **Smart Memory Updates**: LLM-driven ADD/UPDATE/DELETE actions
- **Cross-client Compatibility**: Use OpenMemory with Claude Desktop, Cursor, etc.

### Web Interface
Modern React-based web dashboard located in `./webui/` with:
- Live audio recording and real-time streaming
- Chat interface with conversation management
- **Advanced memory search** with semantic search and relevance threshold filtering
- **Memory count display** showing total memories with live filtering
- **Dual-layer filtering** combining semantic and text search
- System monitoring and debugging tools

### Quick Start

#### 1. Interactive Setup (Recommended)
```bash
# Run interactive setup wizard
./init.sh
```

**The setup wizard guides you through:**
- **Authentication**: Admin email/password setup with secure keys
- **Transcription Provider**: Choose between Deepgram or Offline (Parakeet)
- **LLM Provider**: Choose between OpenAI (recommended) or Ollama for memory extraction
- **Memory Provider**: Choose between Chronicle Native or OpenMemory MCP
- **HTTPS Configuration**: Optional SSL setup for microphone access (uses Caddy)
- **Optional Services**: Speaker Recognition, network configuration
- **API Keys**: Prompts for all required keys with helpful links

#### 2. Start Services 

**HTTP Mode (Default - No SSL required):**
```bash
# Direct service access without nginx proxy
docker compose up --build -d
```
- **Web Dashboard**: http://localhost:5173
- **Backend API**: http://localhost:8000

**HTTPS Mode (For network access and microphone features):**
```bash
# Start with HTTPS (requires Caddy configuration from wizard)
docker compose --profile https up --build -d
```
- **Web Dashboard**: https://localhost/ or https://your-ip/
- **Backend API**: https://localhost/api/ or https://your-ip/api/

#### 3. Access URLs

**Chronicle Advanced Backend (Primary - ports 80/443):**
- **HTTPS Dashboard**: https://localhost/ or https://your-ip/
- **HTTP**: http://localhost/ (redirects to HTTPS)
- **Live Recording**: Available at `/live-record` page

**Speaker Recognition Service (Secondary - ports 8081/8444):**
- **HTTPS Dashboard**: https://localhost:8444/ or https://your-ip:8444/
- **HTTP**: http://localhost:8081/ (redirects to HTTPS)
- **Features**: Speaker enrollment, audio analysis, live inference

**Features available with HTTPS:**
- 🎤 **Live Recording** - Real-time audio streaming with WebSocket
- 🔒 **Secure WebSocket** connections (WSS)
- 🌐 **Network Access** from other devices via Tailscale/LAN
- 🔄 **Automatic protocol detection** - Frontend auto-configures for HTTP/HTTPS

See [Docs/ssl-certificates.md](../../Docs/ssl-certificates.md) for how SSL is configured.

## Testing

### Integration Tests

To run integration tests with different transcription providers:

```bash
# Test with different configurations using config.yml files
# Test configs located in tests/configs/

# Test with Parakeet ASR + Ollama (offline, no API keys)
CONFIG_FILE=../../tests/configs/parakeet-ollama.yml ./run-test.sh

# Test with Deepgram + OpenAI (cloud-based)
CONFIG_FILE=../../tests/configs/deepgram-openai.yml ./run-test.sh

# Manual Robot Framework test execution
source .env && export DEEPGRAM_API_KEY OPENAI_API_KEY && \
  uv run robot --outputdir ../../test-results --loglevel INFO ../../tests/integration/integration_test.robot
```

**Prerequisites:**
- API keys configured in `.env` file (for cloud providers)
- Test configurations in `tests/configs/` directory
- For debugging: Set `CLEANUP_CONTAINERS=false` environment variable to keep containers running
