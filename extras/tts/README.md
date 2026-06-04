# Chronicle TTS Services

Provider-based text-to-speech services for Chronicle. Follows the same architecture as `extras/asr-services/`.

## Quick Start

```bash
# Run setup wizard
cd extras/tts
uv run --with-requirements ../../setup-requirements.txt python init.py

# Start the service (TADA or Fish Speech)
docker compose up tada-tts -d --build
# OR
docker compose up fish-tts -d --build

# Test health
curl http://localhost:8770/health

# Synthesize speech
curl -X POST http://localhost:8770/synthesize \
  -F "text=Hello, this is a test of the TADA text to speech system." \
  -o output.wav

# Synthesize with voice cloning (provide reference audio + transcript)
curl -X POST http://localhost:8770/synthesize \
  -F "text=This will sound like the reference speaker." \
  -F "reference_audio=@reference.wav" \
  -F "reference_text=The transcript of the reference audio clip." \
  -o cloned_output.wav
```

## Available Providers

### TADA (HumeAI)

[TADA](https://github.com/HumeAI/tada) uses 1:1 token alignment between text and audio, eliminating hallucinations by construction. MIT licensed.

| Model | Parameters | Languages | VRAM |
|-------|-----------|-----------|------|
| `HumeAI/tada-1b` | ~2B | English | ~4-5GB |
| `HumeAI/tada-3b-ml` | ~3B+ | 9 languages (ar, zh, de, es, fr, it, ja, pl, pt) | ~7-8GB |

**Capabilities:**
- Zero-shot voice cloning from a reference audio clip
- Speech continuation
- RTF 0.09 (5x+ faster than real-time)
- Zero hallucination rate

### Fish Speech (Fish Audio)

[Fish Speech](https://github.com/fishaudio/fish-speech) uses Dual-AR architecture with 50+ language support and inline emotion/prosody control.

| Model | Parameters | Languages | Size | License |
|-------|-----------|-----------|------|---------|
| `fishaudio/s2-pro` | - | 83 | ~11GB | CC-BY-NC-SA |
| `fishaudio/openaudio-s1-mini` | 0.5B | 50+ | ~6GB | CC-BY-NC-SA |
| `fishaudio/fish-speech-1.5` | Larger | 50+ | ~8GB | Research License |

**Capabilities:**
- Zero-shot voice cloning
- 50+ language support
- Inline emotion/prosody tags: `[laugh]`, `[whispers]`, `[super happy]`, etc.
- Streaming support
- Optional `torch.compile` for ~10x speedup

**Emotion Tags:**

Add emotion tags directly in your text to control prosody:
```
[laugh] That's hilarious! [whispers] But don't tell anyone.
I'm [super happy] to see you today!
```

**Environment Variables:**
```bash
TTS_MODEL=fishaudio/s2-pro              # Model to use (default)
TTS_COMPILE=false                        # torch.compile (~10x speedup, slower warmup)
TTS_HALF=true                            # Half precision (reduces VRAM)
```

### KittenTTS (KittenML)

[KittenTTS](https://github.com/KittenML/KittenTTS) is an ultra-light (~25MB) CPU-only ONNX
TTS — no GPU and no API key required. English only, with preset voices. Good for low-resource
hosts or when GPU is unavailable.

**Capabilities:** lightweight, CPU-only, preset voices.

Started via the `kittentts-tts` service. It uses dedicated `KITTEN_TTS_*` env vars (so the
heavy Fish/TADA settings in `.env` don't bleed into this CPU service):
```bash
KITTEN_TTS_MODEL=KittenML/kitten-tts-mini-0.8   # Model to use
KITTEN_TTS_VOICE=Jasper                          # Preset voice
KITTEN_TTS_SPEED=1.0                             # Speech speed multiplier
KITTEN_TTS_PORT=8770                             # Service port
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/info` | GET | Model info and capabilities |
| `/synthesize` | POST | Generate speech from text |

### POST /synthesize

**Parameters (multipart form):**
- `text` (required): Text to synthesize
- `reference_audio` (optional): WAV file for voice cloning
- `reference_text` (optional): Transcript of the reference audio

**Returns:** WAV audio bytes with headers `X-Sample-Rate`, `X-Provider`, `X-Model`.

## Configuration

See `.env.template` for all available options. Key settings:

```bash
TTS_MODEL=HumeAI/tada-1b      # Model to use
TTS_PORT=8770                   # Service port
PYTORCH_CUDA_VERSION=cu126      # CUDA version
TTS_LANGUAGE=                   # Language (for multilingual model)
```

## Architecture

```
extras/tts/
├── common/                    # Shared abstractions
│   ├── base_service.py       # FastAPI app factory + abstract base class
│   └── response_models.py    # Pydantic models for API responses
├── providers/
│   ├── tada/                 # HumeAI TADA provider (GPU)
│   ├── fish_speech/          # Fish Audio Fish Speech provider (GPU)
│   │   ├── Dockerfile
│   │   ├── startup.py        # Container startup orchestrator
│   │   ├── service.py        # FastAPI service wrapper
│   │   └── synthesizer.py    # HTTP client to fish-speech API
│   └── kittentts/            # KittenML KittenTTS provider (CPU, ~25MB ONNX)
├── docker-compose.yml
├── pyproject.toml
├── init.py                   # Interactive setup script
└── .env.template
```

Adding a new TTS provider requires creating a `providers/{name}/` directory with `service.py`, `synthesizer.py`, and `Dockerfile`.
