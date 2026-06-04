"""Hermes acoustic wake-word service.

Standalone service that consumes the live ``audio:stream:*`` Redis stream,
detects the acoustic "Hermes" wake word, captures the following command turn
via Silero VAD + Smart Turn v3, resolves the command text from the existing
transcription results, and publishes a ``wake_word.detected`` event to the
``wakeword:detections`` Redis stream for the backend to forward to the Hermes
plugin.

Runs a small FastAPI app for health/status alongside the background consumer.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from consumer import DETECTIONS_STREAM, GROUP_NAME, WakeWordConsumer
from detector import HermesDetector
from samples import BUCKETS, SampleStore

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wakeword-service")

# Wake word is "hey hermes" — two words separate from ambient noise far better
# than the single word "hermes" (held-out: 5.4 vs 52 false-positives/hour).
MODEL_PATH = os.getenv("WAKEWORD_MODEL_PATH", "/app/models/hey_hermes.onnx")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
THRESHOLD = float(os.getenv("WAKEWORD_THRESHOLD", "0.9"))
# patience=2 (consecutive frames) is the shippable operating point: 0.9/2 ->
# 5.4 FP/hr at 95.6% recall on held-out noise.
PATIENCE = int(os.getenv("WAKEWORD_PATIENCE", "2"))
DEBOUNCE_SECS = float(os.getenv("WAKEWORD_DEBOUNCE_SECS", "3.0"))
VAD_THRESHOLD = float(os.getenv("WAKEWORD_VAD_THRESHOLD", "0.5"))
# End-of-turn is decided by the Smart Turn MODEL at each speech->silence pause
# (eot_min_silence -> first query, eot_recheck -> re-query cadence). stop_secs is
# now only a LONG backstop for when the model never fires (pure trailing silence).
STOP_SECS = float(os.getenv("WAKEWORD_STOP_SECS", "6.0"))
EOT_MIN_SILENCE_SECS = float(os.getenv("WAKEWORD_EOT_MIN_SILENCE_SECS", "0.2"))
EOT_RECHECK_SECS = float(os.getenv("WAKEWORD_EOT_RECHECK_SECS", "0.3"))
MAX_ARM_SECS = float(os.getenv("WAKEWORD_MAX_ARM_SECS", "15.0"))
SERVICE_PORT = int(os.getenv("WAKEWORD_SERVICE_PORT", "8770"))
# Root for the data-collection clip store (mounted volume in docker-compose).
DATA_DIR = os.getenv("WAKEWORD_DATA_DIR", "/app/data/samples")
# "Prime + say it" capture tuning (data collection).
PRIME_TIMEOUT_SECS = float(os.getenv("WAKEWORD_PRIME_TIMEOUT_SECS", "12.0"))
PRIME_TRAIL_SILENCE_SECS = float(os.getenv("WAKEWORD_PRIME_TRAIL_SILENCE_SECS", "0.6"))
PRIME_MAX_SECS = float(os.getenv("WAKEWORD_PRIME_MAX_SECS", "4.0"))
# Optional explicit ONNX paths (vendored into models/). Empty -> pipecat bundle.
SMART_TURN_MODEL_PATH = os.getenv("SMART_TURN_MODEL_PATH", "") or None
SILERO_VAD_MODEL_PATH = os.getenv("SILERO_VAD_MODEL_PATH", "") or None


def _validate_models() -> None:
    """Refuse to start unless every configured model file exists on disk.

    Only models present in models/ are allowed — a missing/typo'd path fails fast
    with the list of what's actually available rather than half-starting.
    """
    models_dir = os.path.dirname(MODEL_PATH) or "."
    available = sorted(
        f for f in os.listdir(models_dir) if f.endswith(".onnx")
    ) if os.path.isdir(models_dir) else []
    # Smart Turn / Silero default to the pipecat bundle when their env is unset.
    required = {"wake-word model": MODEL_PATH}
    if SMART_TURN_MODEL_PATH:
        required["Smart Turn model"] = SMART_TURN_MODEL_PATH
    if SILERO_VAD_MODEL_PATH:
        required["Silero VAD model"] = SILERO_VAD_MODEL_PATH
    missing = {name: p for name, p in required.items() if not os.path.exists(p)}
    if missing:
        details = "; ".join(f"{name} '{p}'" for name, p in missing.items())
        raise FileNotFoundError(
            f"Configured model(s) not found on disk: {details}. "
            f"Available .onnx in {models_dir}: {available or '(none)'}. "
            f"Train/vendor the model and set the *_MODEL_PATH env vars accordingly."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the detector and start the background consumer."""
    _validate_models()

    detector = HermesDetector(
        model_path=MODEL_PATH,
        threshold=THRESHOLD,
        patience=PATIENCE,
        debounce_secs=DEBOUNCE_SECS,
        vad_threshold=VAD_THRESHOLD,
        stop_secs=STOP_SECS,
        max_arm_secs=MAX_ARM_SECS,
        eot_min_silence_secs=EOT_MIN_SILENCE_SECS,
        eot_recheck_secs=EOT_RECHECK_SECS,
        smart_turn_model_path=SMART_TURN_MODEL_PATH,
        silero_vad_model_path=SILERO_VAD_MODEL_PATH,
        prime_timeout_secs=PRIME_TIMEOUT_SECS,
        prime_trail_silence_secs=PRIME_TRAIL_SILENCE_SECS,
        prime_max_secs=PRIME_MAX_SECS,
    )
    store = SampleStore(DATA_DIR)
    app.state.store = store
    consumer = WakeWordConsumer(detector=detector, redis_url=REDIS_URL, sample_store=store)
    app.state.consumer = consumer
    consumer_task = asyncio.create_task(consumer.start())
    app.state.consumer_task = consumer_task
    logger.info(f"Wake-word service ready (model={MODEL_PATH}, group={GROUP_NAME})")

    try:
        yield
    finally:
        await consumer.stop()
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Hermes Wake-Word Service", lifespan=lifespan)


@app.get("/health")
async def health():
    """Basic liveness + config summary."""
    return {
        "status": "ok",
        "model_path": MODEL_PATH,
        "model_loaded": os.path.exists(MODEL_PATH),
        "redis_url": REDIS_URL,
        "consumer_group": GROUP_NAME,
        "detections_stream": DETECTIONS_STREAM,
        "threshold": THRESHOLD,
    }


@app.get("/status")
async def status():
    """Active stream count from the running consumer."""
    consumer: WakeWordConsumer = app.state.consumer
    active = sum(1 for t in consumer._stream_tasks.values() if not t.done())
    return {
        "running": consumer.running,
        "active_streams": active,
        "known_streams": list(consumer._stream_tasks.keys()),
    }


# Support models that share the models/ dir but are NOT selectable wake words
# (nanowakeword feature front-end, Silero VAD, Smart Turn). Excluded from /models.
_NON_WAKE_MODELS = {"melspectrogram", "embedding_model", "silero_vad"}


def _is_wake_model(name: str) -> bool:
    return name not in _NON_WAKE_MODELS and not name.startswith("smart-turn")


@app.get("/models")
async def models():
    """Wake-word models available on disk, and which one is active.

    Drives the acoustic-wake-word condition picker in the dashboard so it can
    only offer wake words this service actually has — support models (VAD, Smart
    Turn, nanowakeword feature front-end) are filtered out.
    """
    models_dir = os.path.dirname(MODEL_PATH) or "."
    available = (
        sorted(
            name
            for f in os.listdir(models_dir)
            if f.endswith(".onnx") and _is_wake_model(name := os.path.splitext(f)[0])
        )
        if os.path.isdir(models_dir)
        else []
    )
    return {
        "available": available,
        "active": os.path.splitext(os.path.basename(MODEL_PATH))[0],
    }


# --------------------------------------------------------------------------- #
# Data-collection API (the training flywheel): prime positive capture, review
# captured clips, label them true/false positive. Consumed by the backend proxy
# at /api/wakeword/* — see backends/advanced .../routers/modules/wakeword_routes.py
# --------------------------------------------------------------------------- #


class PrimeRequest(BaseModel):
    client_id: str


class LabelRequest(BaseModel):
    label: str  # "wake" -> positive, "not_wake" -> negative


@app.get("/streams")
async def streams():
    """Active audio streams the UI can prime for a positive capture."""
    consumer: WakeWordConsumer = app.state.consumer
    return {"streams": consumer.active_clients()}


@app.post("/prime")
async def prime(req: PrimeRequest):
    """Arm a one-shot positive capture on a streaming client.

    The next utterance on that stream is saved as a labeled positive regardless
    of model score (the false-negative / hard-positive collection path).
    """
    consumer: WakeWordConsumer = app.state.consumer
    if not consumer.prime(req.client_id):
        raise HTTPException(
            status_code=404,
            detail=f"No active stream '{req.client_id}' to prime",
        )
    return {"client_id": req.client_id, "primed": True}


@app.get("/samples")
async def list_samples(bucket: str = Query("pending")):
    """List captured clips in a bucket (pending / positive / negative)."""
    if bucket not in BUCKETS:
        raise HTTPException(status_code=400, detail=f"bad bucket (expected {BUCKETS})")
    store: SampleStore = app.state.store
    return {"bucket": bucket, "samples": store.list(bucket)}


@app.get("/samples/stats")
async def sample_stats():
    """Per-bucket clip counts for the data-collection dashboard."""
    store: SampleStore = app.state.store
    return store.stats()


@app.get("/samples/{clip_id}/audio")
async def sample_audio(clip_id: str):
    """Return a clip's WAV bytes for in-browser playback."""
    store: SampleStore = app.state.store
    path = store.wav_path(clip_id)
    if path is None:
        raise HTTPException(status_code=404, detail="clip not found")
    with open(path, "rb") as fh:
        data = fh.read()
    return Response(content=data, media_type="audio/wav")


@app.post("/samples/{clip_id}/label")
async def label_sample(clip_id: str, req: LabelRequest):
    """Apply a review label, moving the clip into positive/negative."""
    store: SampleStore = app.state.store
    try:
        return store.label(clip_id, req.label)
    except KeyError:
        raise HTTPException(status_code=404, detail="clip not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/samples/{clip_id}")
async def delete_sample(clip_id: str):
    """Delete a clip (WAV + metadata)."""
    store: SampleStore = app.state.store
    if not store.delete(clip_id):
        raise HTTPException(status_code=404, detail="clip not found")
    return {"deleted": clip_id}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
