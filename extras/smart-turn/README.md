# Smart Turn Detection Service

Standalone microservice that predicts whether a speaker's turn is complete, using the [pipecat-ai/smart-turn](https://github.com/pipecat-ai/smart-turn) ONNX model (~8MB, Whisper Tiny encoder + classifier).

- ~12ms CPU inference
- Analyzes up to 8 seconds of audio
- 23 language support
- BSD-2-Clause licensed model

## Quick Start

```bash
# Ensure chronicle-network exists
docker network create chronicle-network 2>/dev/null || true

# Build and start
docker compose up --build -d

# Health check
curl http://localhost:8766/health
```

## API

### `GET /health`

Returns service status.

```json
{"status": "ok", "model": "smart-turn-v3.2-cpu"}
```

### `POST /predict`

Send raw PCM audio (int16, 16kHz, mono) as the request body.

Returns:
```json
{"prediction": 1, "probability": 0.8732}
```

- `prediction`: `1` = turn complete (speaker finished), `0` = turn incomplete
- `probability`: model confidence (0.0 to 1.0)

**Example with Python:**
```python
import numpy as np
import requests

# Load or generate 16kHz mono int16 audio
audio = np.zeros(16000 * 3, dtype=np.int16)  # 3 seconds of silence
response = requests.post("http://localhost:8766/predict", data=audio.tobytes())
print(response.json())
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SMART_TURN_PORT` | `8766` | Service port |
| `MODEL_FILENAME` | `smart-turn-v3.2-cpu.onnx` | ONNX model file |

## How It Works

The model uses a Whisper Tiny encoder to extract audio features from the last 8 seconds of speech, then a classifier head predicts turn completion based on intonation, prosody, and linguistic cues. Audio shorter than 8 seconds is zero-padded at the beginning.
