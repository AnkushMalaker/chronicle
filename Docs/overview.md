# Chronicle Overview

Chronicle is an open-source, self-hosted system for building a personal timeline of your life. It captures events — conversations, audio, images, and more — processes them with AI, and extracts memories and facts that accumulate over time into a personal knowledge base.

The goal is a personal AI that gets better the more you use it: the more context it has about you, the more useful it becomes.

## Core Ideas

- **Timeline of events**: Your life is a sequence of things that happen — someone talks, music plays, a photo is taken. Chronicle models these as timestamped events on a timeline.
- **Multimodal**: Audio is the primary input today, but the architecture supports images, visual context, and other data sources.
- **Memories from everything**: Events produce memories. A conversation yields facts about people, plans, and preferences. A photo yields location, context, and associations.
- **Self-hosted**: Runs on your hardware, your data stays with you.
- **Hackable**: Designed to be forked, modified, and extended. Pluggable providers for transcription, LLM, memory storage, and analysis.

## How It Works

```
Audio/Images/Data  →  Ingestion  →  Processing  →  Memories
                                                      ↓
                                                 Vector Store
                                                      ↓
                                              Retrieval & Search
```

### Audio Pipeline (Primary)

1. **Capture**: OMI devices, microphones, or uploaded files stream audio
2. **Transcription**: Deepgram (cloud) or Parakeet (local) converts speech to text
3. **Speaker Recognition**: Optional identification of who said what (pyannote)
4. **Memory Extraction**: LLM extracts facts, preferences, and context from transcripts
5. **Storage**: Memories stored as vectors in Qdrant for semantic search

### Image Pipeline (In Development)

1. **Import**: Zip upload, or sync from external services (e.g., Immich)
2. **Analysis**: Extract EXIF metadata, captions, detected objects
3. **Memory Extraction**: Same LLM pipeline, different source type
4. **Storage**: Same vector store, queryable alongside conversation memories

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Chronicle System                    │
├─────────────────────────────────────────────────────┤
│                                                       │
│  ┌──────────────┐    ┌──────────────┐  ┌──────────┐ │
│  │ Mobile App   │◄──►│   Backend    │◄►│ MongoDB  │ │
│  │ (React       │    │   (FastAPI)  │  │          │ │
│  │  Native)     │    │              │  └──────────┘ │
│  └──────────────┘    └──────┬───────┘               │
│                             │                        │
│  ┌──────────────┐    ┌──────▼───────┐  ┌──────────┐ │
│  │ Web UI       │    │   Workers    │  │ Qdrant   │ │
│  │ (React)      │    │  (RQ/Redis)  │  │ (Vector) │ │
│  └──────────────┘    └──────────────┘  └──────────┘ │
│                                                       │
│  Transcription:  Deepgram (cloud) or Parakeet (local) │
│  LLM:           OpenAI (cloud) or Ollama (local)      │
│  Optional:      Speaker Recognition, OpenMemory MCP   │
└─────────────────────────────────────────────────────┘
```

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **Backend** | `backends/advanced/` | FastAPI server, audio processing, API |
| **Web UI** | `backends/advanced/webui/` | React dashboard for conversations and memories |
| **Mobile App** | `app/` | React Native app for OMI device pairing |
| **Speaker Recognition** | `extras/speaker-recognition/` | Voice identification service |
| **ASR Services** | `extras/asr-services/` | Local speech-to-text (Parakeet) |
| **OpenMemory MCP** | `extras/openmemory-mcp/` | Cross-client memory compatibility |
| **HAVPE Relay** | `extras/havpe-relay/` | ESP32 audio bridge |

### Pluggable Providers

Chronicle is designed around swappable providers:

- **Transcription**: Deepgram API or local Parakeet ASR
- **LLM**: OpenAI or local Ollama
- **Memory Storage**: Chronicle native (Qdrant) or OpenMemory MCP
- **Speaker Recognition**: pyannote-based service (optional)

## Repository Structure

```
chronicle/
├── app/                     # React Native mobile app
├── backends/
│   ├── advanced/            # Main backend (FastAPI + WebUI)
│   ├── simple/              # Minimal backend for learning
│   └── other-backends/      # Example/alternative implementations
├── extras/
│   ├── speaker-recognition/ # Voice identification
│   ├── asr-services/        # Local ASR (Parakeet)
│   ├── openmemory-mcp/      # External memory server
│   └── havpe-relay/         # ESP32 audio bridge
├── config/                  # Central configuration
├── Docs/                    # Documentation
├── tests/                   # Integration tests (Robot Framework)
├── wizard.py                # Setup wizard
└── services.py              # Service lifecycle manager
```

## Getting Started

See [quickstart.md](../quickstart.md) for setup instructions.

```bash
# Setup
./wizard.sh

# Start
./start.sh

# Access
open http://localhost:5173
```

## Further Reading

- [Quick Start Guide](../quickstart.md) — Step-by-step setup
- [Initialization System](init-system.md) — Setup wizard internals and port configuration
- [Audio Pipeline Architecture](audio-pipeline-architecture.md) — Deep technical reference
- [SSL Certificates](ssl-certificates.md) — HTTPS setup
- [Backend Architecture](../backends/advanced/Docs/architecture.md) — Backend internals
