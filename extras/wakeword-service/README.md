# Hermes Acoustic Wake-Word Service

A standalone Chronicle service that gives Hermes a real **acoustic** wake word.
It consumes the live audio stream, detects "Hermes" acoustically, captures the
command the user speaks afterward using semantic end-of-turn detection, and
forwards it to the existing Hermes agent — running **in parallel** with the
existing text-keyword trigger.

## How it works

```
audio:stream:{client_id}  (Redis, 0.25s PCM frames @ 16kHz/16-bit/mono)
        │  consumer group: wakeword_detection
        ▼
wakeword-service  (this service)
  ├─ NanoInterpreter(hermes.onnx).predict()      → acoustic "Hermes" hit → ARM
  ├─ Silero VAD + Smart Turn v3 (pipecat, ONNX)  → capture command until end-of-turn
  └─ resolve command text from transcription:results:{session_id}  (no 2nd ASR)
        ▼
  XADD wakeword:detections  { client_id, session_id, user_id, command, score, ... }
        ▼
  backend dispatcher → PluginRouter.dispatch_event("wake_word.detected", …)
        ▼
  Hermes plugin → external Hermes agent  /v1/chat/completions
```

Both detection paths converge on the **same** Hermes plugin entry point, so the
acoustic and text triggers reach the agent identically.

### Command-text source

After the wake word arms, the service does **not** run a second ASR. It records
the arm timestamp, lets Silero VAD + Smart Turn v3 detect the end of the
command turn, then reads the existing `transcription:results:{session_id}`
Redis stream and concatenates the transcript chunks whose timestamps fall in the
armed window. This reuses Chronicle's existing transcription.

## Models (in `models/`)

| File | Purpose | Source |
|------|---------|--------|
| `hermes.onnx` | Acoustic wake-word model | trained — see `training/` |
| `smart-turn-v3.2-cpu.onnx` | Semantic end-of-turn | pipecat (vendored) |
| `silero_vad.onnx` | Voice activity detection | pipecat (vendored) |
| `melspectrogram.onnx`, `embedding_model.onnx` | nanowakeword features | nanowakeword (vendored) |

Vendor/refresh the non-trained ONNX files with `./vendor_models.sh`.

## Training the wake word

The acoustic model is trained with `untracked/nanowakeword` on a GPU. See
`training/` for the config and helpers. Output goes to
`extras/wakeword-service/models/hermes.onnx`.

```bash
cd training
# one-time: create venv and install deps (uses the repo's vendored nanowakeword)
uv venv --python 3.12 .venv-train
VIRTUAL_ENV=$(pwd)/.venv-train uv pip install \
    -e "../../../untracked/nanowakeword[train]" setuptools==69.5.1 piper-tts onnxruntime-gpu

# fetch background-noise + negative datasets (multi-GB, see hermes_config.yaml comments)
# then run the full pipeline (generate clips → features → train → ONNX export)
.venv-train/bin/nanowakeword -c hermes_config.yaml
cp trained_models/hermes/model/hermes.onnx ../models/hermes.onnx
```

## Running

```bash
# Requires the backend stack (Redis on chronicle-network) to be up.
docker compose up --build -d

# Health / status
curl http://localhost:8770/health
curl http://localhost:8770/status
```

## Configuration (env)

| Var | Default | Meaning |
|-----|---------|---------|
| `REDIS_URL` | `redis://redis:6379/0` | Shared backend Redis |
| `WAKEWORD_MODEL_PATH` | `/app/models/hermes.onnx` | Wake-word model |
| `WAKEWORD_THRESHOLD` | `0.9` | Acoustic detection threshold (favor precision) |
| `WAKEWORD_DEBOUNCE_SECS` | `3.0` | Suppress repeat arming window |
| `WAKEWORD_VAD_THRESHOLD` | `0.5` | Silero speech threshold |
| `WAKEWORD_STOP_SECS` | `2.0` | Silence that forces end-of-turn |
| `WAKEWORD_MAX_ARM_SECS` | `15.0` | Hard cap on capture duration |
| `WAKEWORD_PORT` | `8770` | Health/status HTTP port |

## Backend wiring

The standalone detector cannot call the in-process plugin router directly, so it
publishes to the `wakeword:detections` Redis stream. The backend runs a small
consumer (`services/wakeword/dispatcher.py`) that reads that stream and calls
`PluginRouter.dispatch_event("wake_word.detected", …)`. The Hermes plugin's
`on_wake_word_detected` handler shares the exact agent-call path as the text
trigger's `on_transcript`.

The text trigger (`keyword_anywhere: [hermes]` in `config/plugins.yml`) stays
active — both paths run in parallel.
