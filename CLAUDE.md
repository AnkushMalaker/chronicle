# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Chronicle is at the core an AI-powered personal system - various devices, including but not limited to wearables from OMI can be used for at the very least audio capture, speaker specific transcription, memory extraction and retrieval.
On top of that - it is being designed to support other services, that can help a user with these inputs such as reminders, action items, personal diagnosis etc.

This supports a comprehensive web dashboard for management.

**⚠️ Active Development Notice**: This project is under active development. Do not create migration scripts or assume stable APIs. Only offer suggestions and improvements when requested.

**❌ No Backward Compatibility**: Do NOT add backward compatibility code unless explicitly requested. This includes fallback logic, legacy field support, or compatibility layers. Always ask before adding backward compatibility - in most cases the answer is no during active development.

## Initial Setup & Configuration

Chronicle includes an **interactive setup wizard** for easy configuration. The wizard guides you through:
- Service selection (backend + optional services)
- Authentication setup (admin account, JWT secrets)
- Transcription provider configuration (Deepgram, Mistral, or offline ASR)
- LLM provider setup (OpenAI or Ollama)
- Memory provider selection (Chronicle Native with Qdrant or OpenMemory MCP)
- Network configuration and HTTPS setup
- Optional services (speaker recognition, Parakeet ASR)

### Quick Start
```bash
# Run the interactive setup wizard from project root
uv run python wizard.py

# Or use the quickstart guide for step-by-step instructions
# See quickstart.md for detailed walkthrough
```

### Setup Documentation
For detailed setup instructions and troubleshooting, see:
- **[@quickstart.md](quickstart.md)**: Beginner-friendly step-by-step setup guide
- **[@Docs/init-system.md](Docs/init-system.md)**: Complete initialization system architecture and design
- **[@Docs/getting-started.md](Docs/getting-started.md)**: Technical quickstart with advanced configuration
- **[@backends/advanced/SETUP_SCRIPTS.md](backends/advanced/SETUP_SCRIPTS.md)**: Setup scripts reference and usage examples
- **[@backends/advanced/Docs/quickstart.md](backends/advanced/Docs/quickstart.md)**: Backend-specific setup guide

### Wizard Architecture
The initialization system uses a **root orchestrator pattern**:
- **`wizard.py`**: Root setup orchestrator for service selection and delegation
- **`backends/advanced/init.py`**: Backend configuration wizard
- **`extras/speaker-recognition/init.py`**: Speaker recognition setup
- **Service setup scripts**: Individual setup for ASR services and OpenMemory MCP

Key features:
- Interactive prompts with validation
- API key masking and secure credential handling
- Environment file generation with placeholders
- HTTPS configuration with SSL certificate generation
- Service status display and health checks
- Automatic backup of existing configurations

## Development Commands

### Backend Development (Advanced Backend - Primary)
```bash
cd backends/advanced

# Start full stack with Docker
docker compose up --build -d

uv run python src/main.py

# Code formatting and linting
uv run black src/
uv run isort src/

# Run tests
uv run pytest
uv run pytest tests/test_memory_service.py  # Single test file

# Run integration tests (local script mirrors CI)
./run-test.sh  # Complete integration test suite

# Environment setup
cp .env.template .env  # Configure environment variables

# Reset data (development)
sudo rm -rf backends/advanced/data/
```

### Testing Infrastructure

#### Local Test Scripts
The project includes simplified test scripts that mirror CI workflows:

```bash
# Run all tests from project root
./run-test.sh [advanced-backend|speaker-recognition|all]

# Advanced backend tests only
./run-test.sh advanced-backend

# Speaker recognition tests only
./run-test.sh speaker-recognition

# Run all test suites (default)
./run-test.sh all
```

#### Advanced Backend Integration Tests
```bash
cd backends/advanced

# Requires .env file with DEEPGRAM_API_KEY and OPENAI_API_KEY
cp .env.template .env  # Configure API keys

# Run full integration test suite
./run-test.sh

# Manual test execution (for debugging)
source .env && export DEEPGRAM_API_KEY && export OPENAI_API_KEY
uv run robot --outputdir test-results --loglevel INFO ../../tests/integration/integration_test.robot

# Leave test containers running for debugging (don't auto-cleanup)
CLEANUP_CONTAINERS=false source .env && export DEEPGRAM_API_KEY && export OPENAI_API_KEY
uv run robot --outputdir test-results --loglevel INFO ../../tests/integration/integration_test.robot

# Manual cleanup when needed
docker compose -f docker-compose-test.yml down -v
```

#### Test Configuration Flags
- **CLEANUP_CONTAINERS** (default: true): Automatically stop and remove test containers after test completion
  - Set to `false` for debugging: `CLEANUP_CONTAINERS=false ./run-test.sh`
- **REBUILD** (default: true): Force rebuild containers with latest code changes
- **FRESH_RUN** (default: true): Start with clean database and fresh containers
- **TRANSCRIPTION_PROVIDER** (default: deepgram): Choose transcription provider (deepgram or parakeet)

#### Test Environment Variables
Tests use isolated test environment with overridden credentials:
- **Test Database**: `test_db` (MongoDB on port 27018, separate from production)
- **Test Ports**: Backend (8001), Qdrant (6337/6338), WebUI (3001)
- **Test Credentials**:
  - `AUTH_SECRET_KEY`: test-jwt-signing-key-for-integration-tests
  - `ADMIN_EMAIL`: test-admin@example.com
  - `ADMIN_PASSWORD`: test-admin-password-123
- **API Keys**: Loaded from `.env` file (DEEPGRAM_API_KEY, OPENAI_API_KEY)
- **Test Settings**: `DISABLE_SPEAKER_RECOGNITION=true` to prevent segment duplication

#### Test Script Features
- **Environment Compatibility**: Works with both local .env files and CI environment variables
- **Isolated Test Environment**: Separate ports and database prevent conflicts with running services
- **Automatic Cleanup**: Configurable via CLEANUP_CONTAINERS flag (default: true)
- **Colored Output**: Clear progress indicators and error reporting
- **Timeout Protection**: 15-minute timeout for advanced backend, 30-minute for speaker recognition
- **Fresh Testing**: Clean database and containers for each test run

### Mobile App Development
```bash
cd app

# Start Expo development server
npm start

# Platform-specific builds
npm run android
npm run ios
npm run web
```

### Additional Services
```bash
# ASR Services
cd extras/asr-services
docker compose up parakeet-asr   # Offline ASR with Parakeet

# Speaker Recognition (with tests)
cd extras/speaker-recognition
docker compose up --build
./run-test.sh  # Run speaker recognition integration tests

# HAVPE Relay (ESP32 bridge)
cd extras/havpe-relay
docker compose up --build
```

## Architecture Overview

### Key Components
- **Audio Pipeline**: Real-time Opus/PCM → Application-level processing → Deepgram/Mistral transcription → memory extraction
- **Wyoming Protocol**: WebSocket communication uses Wyoming protocol (JSONL + binary) for structured audio sessions
- **Unified Pipeline**: Job-based tracking system for all audio processing (WebSocket and file uploads)
- **Job Tracker**: Tracks pipeline jobs with stage events (audio → transcription → memory) and completion status
- **Task Management**: BackgroundTaskManager tracks all async tasks to prevent orphaned processes
- **Unified Transcription**: Deepgram/Mistral transcription with fallback to offline ASR services
- **Memory System**: Pluggable providers (Chronicle native or OpenMemory MCP)
- **Authentication**: Email-based login with MongoDB ObjectId user system
- **Client Management**: Auto-generated client IDs as `{user_id_suffix}-{device_name}`, centralized ClientManager
- **Data Storage**: MongoDB (`audio_chunks` collection for conversations), vector storage (Qdrant or OpenMemory)
- **Web Interface**: React-based web dashboard with authentication and real-time monitoring

### Service Dependencies
```yaml
Required:
  - MongoDB: User data and conversations
  - FastAPI Backend: Core audio processing
  - LLM Service: Memory extraction and action items (OpenAI or Ollama)

Recommended:
  - Vector Storage: Qdrant (Chronicle provider) or OpenMemory MCP server
  - Transcription: Deepgram, Mistral, or offline ASR services

Optional:
  - Parakeet ASR: Offline transcription service
  - Speaker Recognition: Voice identification service
  - Nginx Proxy: Load balancing and routing
  - OpenMemory MCP: For cross-client memory compatibility
```

## Data Flow Architecture

1. **Audio Ingestion**: OMI devices stream audio via WebSocket using Wyoming protocol with JWT auth
2. **Wyoming Protocol Session Management**: Clients send audio-start/audio-stop events for session boundaries
3. **Application-Level Processing**: Global queues and processors handle all audio/transcription/memory tasks
4. **Speech-Driven Conversation Creation**: User-facing conversations only created when speech is detected
5. **Dual Storage System**: Audio sessions always stored in `audio_chunks`, conversations created in `conversations` collection only with speech
6. **Versioned Processing**: Transcript and memory versions tracked with active version pointers
7. **Memory Processing**: Pluggable providers (Chronicle native with individual facts or OpenMemory MCP delegation)
8. **Memory Storage**: Direct Qdrant (Chronicle) or OpenMemory server (MCP provider)
9. **Action Items**: Automatic task detection with "Simon says" trigger phrases
10. **Audio Optimization**: Speech segment extraction removes silence automatically
11. **Task Tracking**: BackgroundTaskManager ensures proper cleanup of all async operations

### Speech-Driven Architecture

**Core Principle**: Conversations are only created when speech is detected, eliminating noise-only sessions from user interfaces.

**Storage Architecture**:
- **`audio_chunks` Collection**: Always stores audio sessions by `audio_uuid` (raw audio capture)
- **`conversations` Collection**: Only created when speech is detected, identified by `conversation_id`
- **Speech Detection**: Analyzes transcript content, duration, and meaningfulness before conversation creation
- **Automatic Filtering**: No user-facing conversations for silence, noise, or brief audio without speech

**Benefits**:
- Clean user experience with only meaningful conversations displayed
- Reduced noise in conversation lists and memory processing
- Efficient storage utilization for speech-only content
- Automatic quality filtering without manual intervention

## Authentication & Security

- **User System**: Email-based authentication with MongoDB ObjectId user IDs
- **Client Registration**: Automatic `{objectid_suffix}-{device_name}` format
- **Data Isolation**: All data scoped by user_id with efficient permission checking
- **API Security**: JWT tokens required for all endpoints and WebSocket connections
- **Admin Bootstrap**: Automatic admin account creation with ADMIN_EMAIL/ADMIN_PASSWORD

## Configuration

### Required Environment Variables
```bash
# Authentication
AUTH_SECRET_KEY=your-super-secret-jwt-key-here
ADMIN_PASSWORD=your-secure-admin-password
ADMIN_EMAIL=admin@example.com

# LLM Configuration
LLM_PROVIDER=openai  # or ollama
OPENAI_API_KEY=your-openai-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini

# Speech-to-Text
DEEPGRAM_API_KEY=your-deepgram-key-here
# Optional: PARAKEET_ASR_URL=http://host.docker.internal:8767
# Optional: TRANSCRIPTION_PROVIDER=deepgram

# Memory Provider
MEMORY_PROVIDER=chronicle  # or openmemory_mcp

# Database
MONGODB_URI=mongodb://mongo:27017
# Database name: chronicle
QDRANT_BASE_URL=qdrant

# Network Configuration
HOST_IP=localhost
BACKEND_PUBLIC_PORT=8000
WEBUI_PORT=5173
CORS_ORIGINS=http://localhost:3000,http://localhost:5173
```

### Memory Provider Configuration

Chronicle supports two pluggable memory backends:

#### Chronicle Memory Provider (Default)
```bash
# Use Chronicle memory provider (default)
MEMORY_PROVIDER=chronicle

# LLM Configuration for memory extraction
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-key-here
OPENAI_MODEL=gpt-4o-mini

# Vector Storage
QDRANT_BASE_URL=qdrant
```

#### OpenMemory MCP Provider
```bash
# Use OpenMemory MCP provider
MEMORY_PROVIDER=openmemory_mcp

# OpenMemory MCP Server Configuration
OPENMEMORY_MCP_URL=http://host.docker.internal:8765
OPENMEMORY_CLIENT_NAME=chronicle
OPENMEMORY_USER_ID=openmemory
OPENMEMORY_TIMEOUT=30

# OpenAI key for OpenMemory server
OPENAI_API_KEY=your-openai-key-here
```

### Transcription Provider Configuration

Chronicle supports multiple transcription services:

```bash
# Option 1: Deepgram (High quality, recommended)
TRANSCRIPTION_PROVIDER=deepgram
DEEPGRAM_API_KEY=your-deepgram-key-here

# Option 2: Mistral (Voxtral models)
TRANSCRIPTION_PROVIDER=mistral
MISTRAL_API_KEY=your-mistral-key-here
MISTRAL_MODEL=voxtral-mini-2507

# Option 3: Local ASR (Parakeet)
PARAKEET_ASR_URL=http://host.docker.internal:8767
```

### Additional Service Configuration
```bash
# LLM Processing
OLLAMA_BASE_URL=http://ollama:11434

# Speaker Recognition
SPEAKER_SERVICE_URL=http://speaker-recognition:8085
```

## Quick API Reference

### Common Endpoints
- **GET /health**: Basic application health check
- **GET /readiness**: Service dependency validation
- **WS /ws_pcm**: Primary audio streaming endpoint (Wyoming protocol + raw PCM fallback)
- **GET /api/conversations**: User's conversations with transcripts
- **GET /api/memories/search**: Semantic memory search with relevance scoring
- **POST /auth/jwt/login**: Email-based login (returns JWT token)

### Authentication Flow
```bash
# 1. Get auth token
curl -s -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@example.com&password=your-password-here" \
  http://localhost:8000/auth/jwt/login

# 2. Use token in API calls
curl -s -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8000/api/conversations
```

### Development Reset Commands
```bash
# Reset all data (development only)
cd backends/advanced
sudo rm -rf data/

# Reset Docker volumes
docker compose down -v
docker compose up --build -d
```

## Development Notes

### Package Management
- **Backend**: Uses `uv` for Python dependency management (faster than pip)
- **Mobile**: Uses `npm` with React Native and Expo
- **Docker**: Primary deployment method with docker-compose

### Testing Strategy
- **Local Test Scripts**: Simplified scripts (`./run-test.sh`) mirror CI workflows for local development
- **End-to-End Integration**: Robot Framework tests (`tests/integration/integration_test.robot`) validate complete audio processing pipeline
- **Speaker Recognition Tests**: `test_speaker_service_integration.py` validates speaker identification
- **Environment Flexibility**: Tests work with both local .env files and CI environment variables
- **Automated Cleanup**: Test containers are automatically removed after execution
- **CI/CD Integration**: GitHub Actions use the same local test scripts for consistency

### Code Style
- **Python**: Black formatter with 100-character line length, isort for imports
- **TypeScript**: Standard React Native conventions
- **Import Guidelines**:
  - NEVER import modules in the middle of functions or files
  - ALL imports must be at the top of the file after the docstring
  - Use lazy imports sparingly and only when absolutely necessary for circular import issues
  - Group imports: standard library, third-party, local imports
- **Error Handling Guidelines**:
  - **Always raise errors, never silently ignore**: Use explicit error handling with proper exceptions rather than silent failures
  - **Understand data structures**: Research and understand input/response or class structure instead of adding defensive `hasattr()` checks

### Docker Build Cache Management
- **Default Behavior**: Docker automatically detects file changes in Dockerfile COPY/ADD instructions and invalidates cache as needed
- **No --no-cache by Default**: Only use `--no-cache` when explicitly needed (e.g., package updates, dependency issues)
- **Smart Caching**: Docker checks file modification times and content hashes to determine when rebuilds are necessary
- **Development Efficiency**: Trust Docker's cache system - it handles most development scenarios correctly

### Health Monitoring
The system includes comprehensive health checks:
- `/readiness`: Service dependency validation
- `/health`: Basic application status
- Memory debug system for transcript processing monitoring

### Integration Test Infrastructure
- **Unified Test Scripts**: Local `./run-test.sh` scripts mirror GitHub Actions workflows
- **Test Environment**: `docker-compose-test.yml` provides isolated services on separate ports
- **Test Database**: Uses `test_db` database with isolated collections
- **Service Ports**: Backend (8001), MongoDB (27018), Qdrant (6335/6336), WebUI (5174)
- **Test Credentials**: Auto-generated `.env.test` files with secure test configurations
- **Ground Truth**: Expected transcript established via `scripts/test_deepgram_direct.py`
- **AI Validation**: OpenAI-powered transcript similarity comparison
- **Test Audio**: 4-minute glass blowing tutorial (`extras/test-audios/DIY*mono*.wav`)
- **CI Compatibility**: Same test logic runs locally and in GitHub Actions

### Cursor Rule Integration
Project includes `.cursor/rules/always-plan-first.mdc` requiring understanding before coding. Always explain the task and confirm approach before implementation.

## Extended Documentation

For detailed technical documentation, see:
- **[@docs/wyoming-protocol.md](docs/wyoming-protocol.md)**: WebSocket communication protocol details
- **[@docs/memory-providers.md](docs/memory-providers.md)**: In-depth memory provider comparison and setup
- **[@docs/versioned-processing.md](docs/versioned-processing.md)**: Transcript and memory versioning details
- **[@docs/api-reference.md](docs/api-reference.md)**: Complete endpoint documentation with examples
- **[@docs/speaker-recognition.md](docs/speaker-recognition.md)**: Advanced analysis and live inference features
- **[@docs/distributed-deployment.md](docs/distributed-deployment.md)**: Multi-machine deployment with Tailscale

## Robot Framework Testing

**IMPORTANT: When writing or modifying Robot Framework tests, you MUST follow the testing guidelines.**

Before writing any Robot Framework test:
1. **Read [@tests/TESTING_GUIDELINES.md](tests/TESTING_GUIDELINES.md)** for comprehensive testing patterns and standards
2. **Check [@tests/tags.md](tests/tags.md)** for approved tags - ONLY 11 tags are permitted
3. **SCAN existing resource files** for keywords - NEVER write code that duplicates existing keywords
4. **Follow the Arrange-Act-Assert pattern** with inline verifications (not abstracted to keywords)

Key Testing Rules:
- **Check Existing Keywords FIRST**: Before writing ANY test code, scan relevant resource files (`websocket_keywords.robot`, `queue_keywords.robot`, `conversation_keywords.robot`, etc.) for existing keywords
- **Tags**: ONLY use the 11 approved tags from tags.md, tab-separated (e.g., `[Tags]    infra	audio-streaming`)
- **Verifications**: Write assertions directly in tests, not in resource keywords
- **Keywords**: Only create keywords for reusable setup/action operations AFTER confirming no existing keyword exists
- **Resources**: Always check existing resource files before creating new keywords or duplicating logic
- **Naming**: Use descriptive names that explain business purpose, not technical implementation

**DO NOT:**
- Write inline code without checking if a keyword already exists for that operation
- Create custom tags (use only the 11 approved tags)
- Abstract verifications into keywords (keep them inline in tests)
- Use space-separated tags (must be tab-separated)
- Skip reading the guidelines before writing tests

## Notes for Claude
Check if the src/ is volume mounted. If not, do compose build so that code changes are reflected. Do not simply run `docker compose restart` as it will not rebuild the image.
Check backends/advanced/Docs for up to date information on advanced backend.
All docker projects have .dockerignore following the exclude pattern. That means files need to be included for them to be visible to docker.
The uv package manager is used for all python projects. Wherever you'd call `python3 main.py` you'd call `uv run python main.py`

**Docker Build Guidelines:**
- Use `docker compose build` without `--no-cache` by default for faster builds
- Only use `--no-cache` when explicitly needed (e.g., if cached layers are causing issues or when troubleshooting build problems)
- Docker's build cache is efficient and saves significant time during development

- Remember that whenever there's a python command, you should use uv run python3 instead