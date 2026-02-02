# ASR Services

Provider-based Automatic Speech Recognition (ASR) services for Chronicle.

## Overview

ASR Services uses a **provider-based architecture** where inference engines are generic and models are configurable at runtime. This allows:

- Adding new Whisper variants without code changes
- Swapping models via environment variables
- Smaller, focused Docker images per inference engine

## Providers

| Provider | Engine | Use Case | Example Models |
|----------|--------|----------|----------------|
| `faster-whisper` | CTranslate2 | Fast Whisper inference (4-6x faster) | Whisper Large V3, distil-whisper |
| `transformers` | HuggingFace | General ASR models | Hindi Whisper, fine-tuned Whisper |
| `vibevoice` | VibeVoice | Speaker diarization | microsoft/VibeVoice-ASR |
| `nemo` | NVIDIA NeMo | NeMo models, long audio | Parakeet, Canary |

## Quick Start

### Option 1: Interactive Setup

```bash
# Run the setup wizard
uv run python init.py

# Follow prompts to select provider and model
```

### Option 2: Use Pre-configured Profile

```bash
# Use Parakeet (NeMo)
cp configs/parakeet.env .env
docker compose up --build -d nemo-asr

# Use Whisper Large V3 (faster-whisper)
cp configs/whisper-large-v3.env .env
docker compose up --build -d faster-whisper-asr

# Use VibeVoice (dedicated provider with speaker diarization)
cp configs/vibevoice.env .env
docker compose up --build -d vibevoice-asr
```

### Option 3: Command Line Configuration

```bash
# Faster-Whisper with custom model
ASR_MODEL=Systran/faster-whisper-large-v3 docker compose up -d faster-whisper-asr

# NeMo with Parakeet
ASR_MODEL=nvidia/parakeet-tdt-0.6b-v3 docker compose up -d nemo-asr

# VibeVoice with speaker diarization
docker compose up -d vibevoice-asr
```

## Available Models

### Faster-Whisper Provider (Recommended for General Use)

| Model | Description | Speed |
|-------|-------------|-------|
| `Systran/faster-whisper-large-v3` | Best quality | ~4x realtime |
| `Systran/faster-whisper-small` | Lightweight | ~10x realtime |
| `deepdml/faster-whisper-large-v3-turbo-ct2` | Speed optimized | ~6x realtime |

### Transformers Provider (Fine-tuned Models)

| Model | Description | Features |
|-------|-------------|----------|
| `Oriserve/Whisper-Hindi2Hinglish-Prime` | Hindi/Hinglish optimized | Fine-tuned for code-switching |
| `openai/whisper-large-v3` | Original Whisper | Baseline comparison |

### VibeVoice Provider (Speaker Diarization)

| Model | Description | Features |
|-------|-------------|----------|
| `microsoft/VibeVoice-ASR` | 7B model with diarization | Speaker identification, 60-min audio |

### NeMo Provider (Long Audio Processing)

| Model | Description | Features |
|-------|-------------|----------|
| `nvidia/parakeet-tdt-0.6b-v3` | Production-ready | Enhanced chunking, timestamps |
| `nvidia/canary-1b` | Multilingual | 1B parameters |

## API Endpoints

All providers expose the **same API**:

### POST /transcribe

Transcribe uploaded audio file.

```bash
curl -X POST http://localhost:8767/transcribe \
  -F "file=@audio.wav"
```

**Response:**
```json
{
  "text": "Hello world",
  "words": [
    {"word": "Hello", "start": 0.0, "end": 0.5, "confidence": 0.98},
    {"word": "world", "start": 0.6, "end": 1.0, "confidence": 0.95}
  ],
  "segments": [
    {"text": "Hello world", "start": 0.0, "end": 1.0}
  ],
  "language": "en",
  "duration": 1.0
}
```

**Response with diarization** (VibeVoice):
```json
{
  "text": "Hello, how are you? I'm fine, thanks!",
  "segments": [
    {"text": "Hello, how are you?", "start": 0.0, "end": 3.5, "speaker": "Speaker 0"},
    {"text": "I'm fine, thanks!", "start": 3.5, "end": 7.2, "speaker": "Speaker 1"}
  ],
  "speakers": [
    {"id": "Speaker 0", "start": 0.0, "end": 3.5},
    {"id": "Speaker 1", "start": 3.5, "end": 7.2}
  ],
  "duration": 7.2
}
```
Note: VibeVoice doesn't provide word-level timestamps, only segment-level with speaker IDs.

### GET /health

Health check endpoint.

```bash
curl http://localhost:8767/health
```

**Response:**
```json
{
  "status": "healthy",
  "model": "Systran/faster-whisper-large-v3",
  "provider": "faster-whisper"
}
```

### GET /info

Service information.

```bash
curl http://localhost:8767/info
```

**Response:**
```json
{
  "model_id": "Systran/faster-whisper-large-v3",
  "provider": "faster-whisper",
  "capabilities": ["timestamps", "word_timestamps", "language_detection", "vad_filter"]
}
```

## Configuration

### Environment Variables

**Common:**
```bash
ASR_MODEL=Systran/faster-whisper-large-v3  # Model identifier
ASR_PORT=8767                               # Service port
```

**Faster-Whisper:**
```bash
COMPUTE_TYPE=float16   # Quantization: float16, int8, float32
DEVICE=cuda            # Device: cuda, cpu
VAD_FILTER=true        # Voice Activity Detection
LANGUAGE=              # Force language (empty for auto-detect)
```

**Transformers:**
```bash
TORCH_DTYPE=float16           # PyTorch dtype
USE_FLASH_ATTENTION=false     # Flash Attention 2
DEVICE=cuda                   # Device: cuda, cpu
```

**VibeVoice:**
```bash
VIBEVOICE_LLM_MODEL=Qwen/Qwen2.5-7B  # LLM backbone for processor
VIBEVOICE_ATTN_IMPL=sdpa             # Attention: sdpa, flash_attention_2, eager
TORCH_DTYPE=bfloat16                 # Recommended dtype for VibeVoice
MAX_NEW_TOKENS=8192                  # Max tokens for generation
DEVICE=cuda                          # Device: cuda, cpu (GPU required, 16GB+ VRAM)
```

VibeVoice raw model output (for debugging):
```json
[{"Start": 0.0, "End": 3.5, "Speaker": 0, "Content": "Hello"}]
```
Keys are normalized: `Start`→`start`, `End`→`end`, `Speaker`→`speaker`, `Content`→`text`

**NeMo:**
```bash
CHUNKING_ENABLED=true          # Enable chunking for long audio
MIN_AUDIO_FOR_CHUNKING=60.0    # Threshold for chunking (seconds)
CHUNK_DURATION_SECONDS=30.0    # Chunk size
PYTORCH_CUDA_VERSION=cu126     # CUDA version for build
```

### Config Profiles

Pre-configured `.env` files in `configs/`:

- `parakeet.env` - NeMo + Parakeet
- `whisper-large-v3.env` - Faster-Whisper + Large V3
- `whisper-hindi.env` - Transformers + Hindi Whisper
- `vibevoice.env` - VibeVoice provider with speaker diarization

## Integration with Chronicle

Configure the Chronicle backend to use your ASR service:

```bash
# In backends/advanced/.env
PARAKEET_ASR_URL=http://host.docker.internal:8767
```

The backend will use this for:
- Fallback when cloud transcription is unavailable
- Offline transcription mode
- Local processing without API keys

## Architecture

```
extras/asr-services/
├── common/                    # Shared utilities
│   ├── base_service.py        # Abstract base class
│   ├── audio_utils.py         # Audio processing
│   └── response_models.py     # Pydantic models
├── providers/
│   ├── faster_whisper/        # CTranslate2 backend
│   ├── transformers/          # HuggingFace backend
│   ├── vibevoice/             # Microsoft VibeVoice backend
│   └── nemo/                  # NVIDIA NeMo backend
├── configs/                   # Pre-configured profiles
├── docker-compose.yml         # All provider services
└── init.py                    # Setup wizard
```

### Key Design Decisions

1. **Provider-based, not model-based**: Docker images are per inference engine
2. **Runtime model selection**: `ASR_MODEL` env var configures which model to load
3. **Unified API**: All providers expose identical endpoints
4. **Config profiles**: Pre-made .env files for common setups

## Development

### Local Development

```bash
# Install dependencies for specific provider
uv sync --group faster-whisper

# Run service directly
uv run python -m providers.faster_whisper.service --port 8765
```

### Testing

```bash
# Test transcription
curl -X POST http://localhost:8767/transcribe \
  -F "file=@test.wav"

# Test health
curl http://localhost:8767/health
```

### Adding New Models

For models compatible with existing providers, just change `ASR_MODEL`:

```bash
# Use a custom model
ASR_MODEL=your-org/your-model docker compose up -d faster-whisper-asr
```

For new inference engines, add a new provider in `providers/`.

## Performance

| Provider | Typical Latency | Memory | GPU Required |
|----------|-----------------|--------|--------------|
| faster-whisper | ~150ms/s | 4GB | Recommended |
| transformers | ~200ms/s | 8GB | Required for large models |
| vibevoice | ~300ms/s | 16GB+ | Required (large 7B model) |
| nemo | ~100ms/s | 6GB | Required |

## Troubleshooting

### Service not starting

```bash
# Check container logs
docker compose logs nemo-asr

# Verify GPU availability
docker run --gpus all nvidia/cuda:12.1-base-ubuntu22.04 nvidia-smi
```

### Model download issues

Models are cached in `./model_cache`. To reset:

```bash
rm -rf model_cache
docker compose up --build -d nemo-asr
```

### Memory errors

Try:
- Use a smaller model (e.g., `Systran/faster-whisper-small`)
- Use quantization (`COMPUTE_TYPE=int8`)
- Check GPU memory with `nvidia-smi`

## Legacy Support

The original `parakeet-asr` service is still available for backward compatibility:

```bash
docker compose up --build -d parakeet-asr
```

This uses the original `Dockerfile_Parakeet` and `parakeet-offline.py`.
