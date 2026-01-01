# Getting Started

# Chronicle Backend Quickstart Guide

> 📖 **New to chronicle?** This is your starting point! After reading this, continue with [architecture.md](./architecture.md) for technical details.

## Overview

Chronicle is an eco-system of services to support "AI wearable" agents/functionality.
At the moment, the basic functionalities are:
- Audio capture (via WebSocket, from OMI device, files, or a laptop)
- Audio transcription
- **Advanced memory system** with pluggable providers (Chronicle native or OpenMemory MCP)
- **Enhanced memory extraction** with individual fact storage and smart updates
- **Semantic memory search** with relevance threshold filtering and live results
- Action item extraction
- Modern React web dashboard with live recording and advanced search features
- Comprehensive user management with JWT authentication

**Core Implementation**: See `src/advanced_omi_backend/main.py` for the complete FastAPI application and WebSocket handling.

## Prerequisites

- Docker and Docker Compose
- API keys for your chosen providers (see setup script)

## Quick Start

### Step 1: Interactive Setup (Recommended)

Run the interactive setup wizard to configure all services with guided prompts:
```bash
cd backends/advanced
./init.sh
```

**The setup wizard will guide you through:**
- **Authentication**: Admin email/password setup
- **Transcription Provider**: Choose Deepgram, Mistral, or Offline (Parakeet)
- **LLM Provider**: Choose OpenAI or Ollama for memory extraction
- **Memory Provider**: Choose Chronicle Native or OpenMemory MCP
- **Optional Services**: Speaker Recognition and other extras
- **Network Configuration**: Ports and host settings

**Example flow:**
```
🚀 Chronicle Interactive Setup
===============================================

► Authentication Setup
----------------------
Admin email [admin@example.com]: john@company.com
Admin password (min 8 chars): ********

► Speech-to-Text Configuration  
-------------------------------
Choose your transcription provider:
  1) Deepgram (recommended - high quality, requires API key)
  2) Mistral (Voxtral models - requires API key) 
  3) Offline (Parakeet ASR - requires GPU, runs locally)
  4) None (skip transcription setup)
Enter choice (1-4) [1]: 1

Get your API key from: https://console.deepgram.com/
Deepgram API key: dg_xxxxxxxxxxxxx

► LLM Provider Configuration
----------------------------
Choose your LLM provider for memory extraction:
  1) OpenAI (GPT-4, GPT-3.5 - requires API key)
  2) Ollama (local models - requires Ollama server)
  3) Skip (no memory extraction)
Enter choice (1-3) [1]: 1
```

### Step 2: HTTPS Setup (Optional)

For microphone access and secure connections, set up HTTPS:
```bash
cd backends/advanced
./setup-https.sh 100.83.66.30  # Your Tailscale/network IP
```

This creates SSL certificates and configures nginx for secure access.

### Step 3: Start the System

**Start all services:**
```bash
cd backends/advanced
docker compose up --build -d
```

This starts:
- **Backend API**: `http://localhost:8000`  
- **Web Dashboard**: `http://localhost:5173`
- **MongoDB**: `localhost:27017`
- **Qdrant**: `localhost:6333`

### Step 4: Optional Services

**If you configured optional services during setup, start them:**

```bash
# OpenMemory MCP (if selected)
cd ../../extras/openmemory-mcp && docker compose up -d

# Parakeet ASR (if selected for offline transcription)  
cd ../../extras/asr-services && docker compose up parakeet -d

# Speaker Recognition (if enabled)
cd ../../extras/speaker-recognition && docker compose up --build -d
```

### Manual Configuration (Alternative)

If you prefer manual configuration, copy the `.env.template` file to `.env` and configure the required values:

**Required Environment Variables:**
```bash
AUTH_SECRET_KEY=your-super-secret-jwt-key-here
ADMIN_PASSWORD=your-secure-admin-password
ADMIN_EMAIL=admin@example.com
```

**Memory Provider Configuration:**
```bash
# Memory Provider (Choose One)
# Option 1: Chronicle Native (Default - Recommended)
MEMORY_PROVIDER=chronicle

# Option 2: OpenMemory MCP (Cross-client compatibility)
# MEMORY_PROVIDER=openmemory_mcp
# OPENMEMORY_MCP_URL=http://host.docker.internal:8765
# OPENMEMORY_CLIENT_NAME=chronicle
# OPENMEMORY_USER_ID=openmemory
```

**LLM Configuration (Choose One):**
```bash
# Option 1: OpenAI (Recommended for best memory extraction)
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-api-key-here
OPENAI_MODEL=gpt-4o-mini

# Option 2: Local Ollama
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama:11434
```

**Transcription Services (Choose One):**
```bash
# Option 1: Deepgram (Recommended for best transcription quality)
TRANSCRIPTION_PROVIDER=deepgram
DEEPGRAM_API_KEY=your-deepgram-api-key-here

# Option 2: Mistral (Voxtral models for transcription)
TRANSCRIPTION_PROVIDER=mistral
MISTRAL_API_KEY=your-mistral-api-key-here
MISTRAL_MODEL=voxtral-mini-2507

# Option 3: Local ASR service  
PARAKEET_ASR_URL=http://host.docker.internal:8080
```

**Important Notes:**
- **OpenAI is strongly recommended** for LLM processing as it provides much better memory extraction and eliminates JSON parsing errors
- **TRANSCRIPTION_PROVIDER** determines which service to use:
  - `deepgram`: Uses Deepgram's Nova-3 model for high-quality transcription
  - `mistral`: Uses Mistral's Voxtral models for transcription
  - If not set, system falls back to offline ASR service
- The system requires either online API keys or offline ASR service configuration

### Testing Your Setup (Optional)

After configuration, verify everything works with the integration test suite:
```bash
./run-test.sh

# Alternative: Manual test with detailed logging
source .env && export DEEPGRAM_API_KEY OPENAI_API_KEY && \
  uv run robot --outputdir ../../test-results --loglevel INFO ../../tests/integration/integration_test.robot
```
This end-to-end test validates the complete audio processing pipeline using Robot Framework.

## Using the System

### Web Dashboard

1. Open `http://localhost:5173`
2. **Login** using the sidebar:
   - **Admin**: `admin@example.com` / `your-admin-password`
   - **Create new users** via admin interface

### Dashboard Features

- **Conversations**: View audio recordings, transcripts, and cropped audio
- **Memories**: Advanced memory search with semantic search, relevance threshold filtering, and memory count display
- **Live Recording**: Real-time audio recording with WebSocket streaming (HTTPS required)
- **User Management**: Create/delete users and their data
- **Client Management**: View active connections and close conversations
- **System Monitoring**: Debug tools and system health monitoring

### Audio Client Connection

Connect audio clients via WebSocket with authentication:

**WebSocket URLs:**
```javascript
// Opus audio stream
ws://your-server-ip:8000/ws?token=YOUR_JWT_TOKEN&device_name=YOUR_DEVICE_NAME

// PCM audio stream  
ws://your-server-ip:8000/ws_pcm?token=YOUR_JWT_TOKEN&device_name=YOUR_DEVICE_NAME
```

**Authentication Methods:**
The system uses email-based authentication with JWT tokens:

```bash
# Login with email
curl -X POST "http://localhost:8000/auth/jwt/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@example.com&password=your-admin-password"

# Response: {"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "bearer"}
```

**Authentication Flow:**
1. **User Registration**: Admin creates users via API or dashboard
2. **Login**: Users authenticate with email and password
3. **Token Usage**: Include JWT token in API calls and WebSocket connections
4. **Data Access**: Users can only access their own data (admins see all)

For detailed authentication documentation, see [`auth.md`](./auth.md).

**Create User Account:**
```bash
export ADMIN_TOKEN="your-admin-token"

# Create user
curl -X POST "http://localhost:8000/api/create_user" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "userpass", "display_name": "John Doe"}'

# Response includes the user_id (MongoDB ObjectId)
# {"message": "User user@example.com created successfully", "user": {"id": "507f1f77bcf86cd799439011", ...}}
```

**Client ID Format:**
The system automatically generates client IDs using the last 6 characters of the MongoDB ObjectId plus device name (e.g., `439011-phone`, `439011-desktop`). This ensures proper user-client association and data isolation.

## Add Existing Data

### Audio File Upload & Processing

The system supports processing existing audio files through the file upload API. This allows you to import and process pre-recorded conversations without requiring a live WebSocket connection.

**Upload and Process WAV Files:**
```bash
export USER_TOKEN="your-jwt-token"

# Upload single WAV file
curl -X POST "http://localhost:8000/api/process-audio-files" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -F "files=@/path/to/audio.wav" \
  -F "device_name=file_upload"

# Upload multiple WAV files
curl -X POST "http://localhost:8000/api/process-audio-files" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -F "files=@/path/to/recording1.wav" \
  -F "files=@/path/to/recording2.wav" \
  -F "device_name=import_batch"
```

**Response Example:**
```json
{
  "message": "Successfully processed 2 audio files",
  "processed_files": [
    {
      "filename": "recording1.wav",
      "sample_rate": 16000,
      "channels": 1,
      "duration_seconds": 120.5,
      "size_bytes": 3856000
    },
    {
      "filename": "recording2.wav", 
      "sample_rate": 44100,
      "channels": 2,
      "duration_seconds": 85.2,
      "size_bytes": 7532800
    }
  ],
  "client_id": "user01-import_batch"
}
```

## System Features

### Audio Processing
- **Real-time streaming**: WebSocket audio ingestion
- **Multiple formats**: Opus and PCM audio support
- **Per-client processing**: Isolated conversation management
- **Speech detection**: Automatic silence removal
- **Audio cropping**: Extract only speech segments

**Implementation**: See `src/advanced_omi_backend/main.py` for WebSocket endpoints and `src/advanced_omi_backend/processors.py` for audio processing pipeline.

### Transcription Options
- **Deepgram API**: Cloud-based batch processing, high accuracy (recommended)
- **Mistral API**: Voxtral models for transcription with REST API processing
- **Self-hosted ASR**: Local Wyoming protocol services with real-time processing
- **Collection timeout**: 1.5 minute collection for optimal online processing quality

### Conversation Management
- **Automatic chunking**: 60-second audio segments
- **Conversation timeouts**: Auto-close after 1.5 minutes of silence
- **Speaker identification**: Track multiple speakers per conversation
- **Manual controls**: Close conversations via API or dashboard

### Memory & Intelligence

#### Pluggable Memory System
- **Two memory providers**: Choose between Chronicle native or OpenMemory MCP
- **Chronicle Provider**: Full control with custom extraction, individual fact storage, smart deduplication
- **OpenMemory MCP Provider**: Cross-client compatibility (Claude Desktop, Cursor, Windsurf), professional processing

#### Enhanced Memory Processing
- **Individual fact storage**: No more generic transcript fallbacks
- **Smart memory updates**: LLM-driven ADD/UPDATE/DELETE actions
- **Enhanced prompts**: Improved fact extraction with granular, specific memories
- **User-centric storage**: All memories keyed by database user_id
- **Semantic search**: Vector-based memory retrieval with embeddings
- **Configurable extraction**: YAML-based configuration for memory extraction
- **Debug tracking**: SQLite-based tracking of transcript → memory conversion
- **Client metadata**: Device information preserved for debugging and reference
- **User isolation**: All data scoped to individual users with multi-device support

**Implementation**: 
- **Memory System**: `src/advanced_omi_backend/memory/memory_service.py` + `src/advanced_omi_backend/controllers/memory_controller.py`
- **Configuration**: memory settings in `config/config.yml` (memory section)

### Authentication & Security
- **Email Authentication**: Login with email and password
- **JWT tokens**: Secure API and WebSocket authentication with 1-hour expiration
- **Role-based access**: Admin vs regular user permissions
- **Data isolation**: Users can only access their own data
- **Client ID Management**: Automatic client-user association via `objectid_suffix-device_name` format
- **Multi-device support**: Single user can connect multiple devices
- **Security headers**: Proper CORS, cookie security, and token validation

**Implementation**: See `src/advanced_omi_backend/auth.py` for authentication logic, `src/advanced_omi_backend/users.py` for user management, and [`auth.md`](./auth.md) for comprehensive documentation.

## Verification

```bash
# System health check
curl http://localhost:8000/health

# Web dashboard
open http://localhost:3000

# View active clients (requires auth token)
curl -H "Authorization: Bearer your-token" http://localhost:8000/api/clients/active
```

## HAVPE Relay Configuration

For ESP32 audio streaming using the HAVPE relay (`extras/havpe-relay/`):

```bash
# Environment variables for HAVPE relay
export AUTH_USERNAME="user@example.com"       # Email address
export AUTH_PASSWORD="your-password"
export DEVICE_NAME="havpe"                    # Device identifier

# Run the relay
cd extras/havpe-relay
python main.py --backend-url http://your-server:8000 --backend-ws-url ws://your-server:8000
```

The relay will automatically:
- Authenticate using `AUTH_USERNAME` (email address)
- Generate client ID as `objectid_suffix-havpe`
- Forward ESP32 audio to the backend with proper authentication
- Handle token refresh and reconnection

## Development tip
uv sync --group (whatever group you want to sync) 
(for example, deepgram, etc.)

## Troubleshooting

**Service Issues:**
- Check logs: `docker compose logs chronicle-backend`
- Restart services: `docker compose restart`
- View all services: `docker compose ps`

**Authentication Issues:**
- Verify `AUTH_SECRET_KEY` is set and long enough (minimum 32 characters)
- Check admin credentials match `.env` file
- Ensure user email/password combinations are correct

**Transcription Issues:**
- **Deepgram**: Verify API key is valid and `TRANSCRIPTION_PROVIDER=deepgram`
- **Mistral**: Verify API key is valid and `TRANSCRIPTION_PROVIDER=mistral`
- **Self-hosted**: Ensure ASR service is running on port 8765
- Check transcription service connection in health endpoint

**Memory Issues:**
- Ensure Ollama is running and model is pulled
- Check Qdrant connection in health endpoint
- Memory processing happens at conversation end

**Connection Issues:**
- Use server's IP address, not localhost for mobile clients
- Ensure WebSocket connections include authentication token
- Check firewall/port settings for remote connections

## Distributed Deployment

### Single Machine vs Distributed Setup

**Single Machine (Default):**
```bash
# Everything on one machine
docker compose up --build -d
```

**Distributed Setup (GPU + Backend separation):**

#### GPU Machine Setup
```bash
# Start GPU-accelerated services
cd extras/asr-services
docker compose up moonshine -d

cd extras/speaker-recognition  
docker compose up --build -d

# Ollama with GPU support
docker run -d --gpus=all -p 11434:11434 \
  -v ollama:/root/.ollama \
  ollama/ollama:latest
```

#### Backend Machine Configuration
```bash
# .env configuration for distributed services
OLLAMA_BASE_URL=http://[gpu-machine-tailscale-ip]:11434
SPEAKER_SERVICE_URL=http://[gpu-machine-tailscale-ip]:8085
PARAKEET_ASR_URL=http://[gpu-machine-tailscale-ip]:8080

# Start lightweight backend services
docker compose up --build -d
```

#### Tailscale Networking
```bash
# Install on each machine
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Find machine IPs
tailscale ip -4
```

**Benefits of Distributed Setup:**
- GPU services on dedicated hardware
- Lightweight backend on VPS/Raspberry Pi
- Automatic Tailscale IP support (100.x.x.x) - no CORS configuration needed
- Encrypted inter-service communication

**Service Examples:**
- GPU machine: LLM inference, ASR, speaker recognition
- Backend machine: FastAPI, WebUI, databases
- Database machine: MongoDB, Qdrant (optional separation)

## Data Architecture

The chronicle backend uses a **user-centric data architecture**:

- **All memories are keyed by database user_id** (not client_id)
- **Client information is stored in metadata** for reference and debugging
- **User email is included** for easy identification in admin interfaces
- **Multi-device support**: Users can access their data from any registered device

For detailed information, see [User Data Architecture](user-data-architecture.md).

## Memory Provider Selection

### Choosing a Memory Provider

Chronicle offers two memory backends:

#### 1. Chronicle Native 
```bash
# In your .env file
MEMORY_PROVIDER=chronicle
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-key-here
```

**Benefits:**
- Full control over memory processing
- Individual fact storage with no fallbacks
- Custom prompts and extraction logic
- Smart deduplication algorithms
- LLM-driven memory updates (ADD/UPDATE/DELETE)
- No external dependencies

#### 2. OpenMemory MCP 
```bash
# First, start the external server
cd extras/openmemory-mcp
docker compose up -d

# Then configure Chronicle
MEMORY_PROVIDER=openmemory_mcp
OPENMEMORY_MCP_URL=http://host.docker.internal:8765
```

**Benefits:**
- Cross-client compatibility (works with Claude Desktop, Cursor, etc.)
- Professional memory processing
- Web UI at http://localhost:8765
- Battle-tested deduplication

**Use OpenMemory MCP when:**
- You want cross-client memory sharing
- You're already using OpenMemory in other tools
- You prefer external expertise over custom logic

**See [MEMORY_PROVIDERS.md](../MEMORY_PROVIDERS.md) for detailed comparison**

## Memory & Action Item Configuration

> 🎯 **New to memory configuration?** Read our [Memory Configuration Guide](./memory-configuration-guide.md) for a step-by-step setup guide with examples.

The system uses **centralized configuration** via `config/config.yml` for all models (LLM, embeddings, vector store) and memory extraction settings.

### Configuration File Location
- **Path**: repository `config/config.yml` (override with `CONFIG_FILE` env var)
- **Hot-reload**: Changes are applied on next processing cycle (no restart required)
- **Fallback**: If file is missing, system uses safe defaults with environment variables

### LLM Provider & Model Configuration

⭐ **OpenAI is STRONGLY RECOMMENDED** for optimal memory extraction performance.

The system supports **multiple LLM providers** - configure via environment variables:

```bash
# In your .env file
LLM_PROVIDER=openai          # RECOMMENDED: Use "openai" for best results
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-4o-mini          # RECOMMENDED: "gpt-5-mini" for better memory extraction

# Alternative: Local Ollama (may have reduced memory quality)
LLM_PROVIDER=ollama          
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=gemma3n:e4b     # Fallback if YAML config fails to load
```

**Why OpenAI is recommended:**
- **Enhanced memory extraction**: Creates multiple granular memories instead of fallback transcripts
- **Better fact extraction**: More reliable JSON parsing and structured output  
- **No more "fallback memories"**: Eliminates generic transcript-based memory entries
- **Improved conversation understanding**: Better context awareness and detail extraction

**YAML Configuration** (provider-specific models):
```yaml
memory_extraction:
  enabled: true
  prompt: |
    Extract anything relevant about this conversation that would be valuable to remember.
    Focus on key topics, people, decisions, dates, and emotional context.
  llm_settings:
    # Model selection based on LLM_PROVIDER:
    # - Ollama: "gemma3n:e4b", "llama3.1:latest", "llama3.2:latest", etc.
    # - OpenAI: "gpt-5-mini" (recommended for JSON reliability), "gpt-5-mini", "gpt-3.5-turbo", etc.
    model: "gemma3n:e4b"
    temperature: 0.1

fact_extraction:
  enabled: false  # Disabled to avoid JSON parsing issues
  # RECOMMENDATION: Enable with OpenAI GPT-4o for better JSON reliability
  llm_settings:
    model: "gemma3n:e4b"  # Auto-switches based on LLM_PROVIDER
    temperature: 0.0  # Lower for factual accuracy
```

**Provider-Specific Behavior:**
- **Ollama**: Uses local models with Ollama embeddings (nomic-embed-text)
- **OpenAI**: Uses OpenAI models with OpenAI embeddings (text-embedding-3-small)
- **Embeddings**: Automatically selected based on provider (768 dims for Ollama, 1536 for OpenAI)

#### Fixing JSON Parsing Errors

If you experience JSON parsing errors in fact extraction:

1. **Switch to OpenAI GPT-4o** (recommended solution):
   ```bash
   # In your .env file
   LLM_PROVIDER=openai
   OPENAI_API_KEY=your-openai-api-key
   OPENAI_MODEL=gpt-4o-mini
   ```

2. **Enable fact extraction** with reliable JSON output:
   ```yaml
   # In config/config.yml (memory section)
   fact_extraction:
     enabled: true  # Safe to enable with GPT-4o
   ```

3. **Monitor logs** for JSON parsing success:
   ```bash
   # Check for JSON parsing errors
   docker logs advanced-backend | grep "JSONDecodeError"
   
   # Verify OpenAI usage
   docker logs advanced-backend | grep "OpenAI response"
   ```

**Why GPT-4o helps with JSON errors:**
- More consistent JSON formatting
- Better instruction following for structured output
- Reduced malformed JSON responses
- Built-in JSON mode for reliable parsing

#### Testing OpenAI Configuration

To verify your OpenAI setup is working:

1. **Check logs for OpenAI usage**:
   ```bash
   # Start the backend and check logs
   docker logs advanced-backend | grep -i "openai"
   
   # You should see:
   # "Using OpenAI provider with model: gpt-5-mini"
   ```

2. **Test memory extraction** with a conversation:
   ```bash
   # The health endpoint includes LLM provider info
   curl http://localhost:8000/health
   
   # Response should include: "llm_provider": "openai"
   ```

3. **Monitor memory processing**:
   ```bash
   # After a conversation ends, check for successful processing
   docker logs advanced-backend | grep "memory processing"
   ```

If you see errors about missing API keys or models, verify your `.env` file has:
```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-your-actual-api-key-here
OPENAI_MODEL=gpt-4o-mini
```

### Quality Control Settings
```yaml
quality_control:
  min_conversation_length: 50      # Skip very short conversations
  max_conversation_length: 50000   # Skip extremely long conversations
  skip_low_content: true           # Skip conversations with mostly filler words
  min_content_ratio: 0.3           # Minimum meaningful content ratio
  skip_patterns:                   # Regex patterns to skip
    - "^(um|uh|hmm|yeah|ok|okay)\\s*$"
    - "^test\\s*$"
    - "^testing\\s*$"
```

### Processing & Performance
```yaml
processing:
  parallel_processing: true        # Enable concurrent processing
  max_concurrent_tasks: 3          # Limit concurrent LLM requests
  processing_timeout: 300          # Timeout for memory extraction (seconds)
  retry_failed: true              # Retry failed extractions
  max_retries: 2                  # Maximum retry attempts
  retry_delay: 5                  # Delay between retries (seconds)
```

### Debug & Monitoring
```yaml
debug:
  enabled: true
  db_path: "/app/debug/memory_debug.db"
  log_level: "INFO"                # DEBUG, INFO, WARNING, ERROR
  log_full_conversations: false    # Privacy consideration
  log_extracted_memories: true     # Log successful extractions
```

### Configuration Validation
The system validates configuration on startup and provides detailed error messages for invalid settings. Use the debug API to verify your configuration:

```bash
# Check current configuration
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8000/api/debug/memory/config
```

### API Endpoints for Debugging
- `GET /api/debug/memory/stats` - Processing statistics
- `GET /api/debug/memory/sessions` - Recent memory sessions
- `GET /api/debug/memory/session/{audio_uuid}` - Detailed session info
- `GET /api/debug/memory/config` - Current configuration
- `GET /api/debug/memory/pipeline/{audio_uuid}` - Pipeline trace

**Implementation**: See `src/advanced_omi_backend/routers/modules/system_routes.py` for debug endpoints and system utilities.

## Next Steps

- **Configure Google OAuth** for easy user login
- **Set up Ollama** for local memory processing
- **Deploy ASR service** for self-hosted transcription
- **Connect audio clients** using the WebSocket API
- **Explore the dashboard** to manage conversations and users
- **Review the user data architecture** for understanding data organization
- **Customize memory extraction** by editing the `memory` section in `config/config.yml`
- **Monitor processing performance** using debug API endpoints
