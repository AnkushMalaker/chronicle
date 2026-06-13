"""Acoustic wake-word service (multi-wake-word).

Standalone service that consumes the live ``audio:stream:*`` Redis stream,
detects one or more acoustic wake words (e.g. "hey hermes" + "hermes") in
parallel, captures the following command turn via Silero VAD + Smart Turn v3,
and publishes a ``wake_word.detected`` event (tagged with which word fired) to
the ``wakeword:detections`` Redis stream for the backend to forward to the
Hermes plugin.

Wake words are configured via ``WAKEWORD_MODELS`` as a comma-separated list of
``name:file`` pairs (file relative to the models dir), e.g.::

    WAKEWORD_MODELS=hey_hermes:hey_hermes_c.onnx,hermes:hermes.onnx

The wake-word NAME (``hey_hermes``) is decoupled from the model FILE
(``hey_hermes_c.onnx``) so a model can be swapped/retrained without renaming its
sample-data directory or its plugin condition. List order is arming PRIORITY when
several words fire on one frame (put the lower-FP word first). Each word may have
a per-deployment second-stage verifier at ``models/<name>_verifier.npz`` (auto-
enabled when present) and per-word threshold/patience overrides.

Runs a small FastAPI app for health/status + the data-collection flywheel API.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from consumer import DETECTIONS_STREAM, GROUP_NAME, WakeWordConsumer
from detector import HermesDetector
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from samples import BUCKETS, PENDING, SampleStore

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wakeword-service")

# Directory holding the wake-word ONNX models (+ optional verifiers).
MODELS_DIR = os.getenv("WAKEWORD_MODELS_DIR", "/app/models")
# Wake words to run, as "name:file" pairs (file relative to MODELS_DIR). Order is
# arming priority. The lower-FP two-word phrase goes first.
WAKEWORD_MODELS = os.getenv("WAKEWORD_MODELS", "hey_hermes:hey_hermes_c.onnx")
# Pre-multi-wake-word flat sample dirs (data/samples/<bucket>) are all "hey
# hermes" — relocate them under this word so they don't contaminate a second one.
LEGACY_WAKEWORD = os.getenv("WAKEWORD_LEGACY", "hey_hermes")
# Words in collect-only (shadow) mode fire to farm false-positive review data but
# never dispatch a command / play a tone / block a real wake word. Use this to run
# a not-yet-trusted word (e.g. the FP-prone single "hermes") live for data only.
WAKEWORD_COLLECT_ONLY = os.getenv("WAKEWORD_COLLECT_ONLY", "")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# Default operating point (per-word overrides via WAKEWORD_THRESHOLDS / _PATIENCES).
THRESHOLD = float(os.getenv("WAKEWORD_THRESHOLD", "0.9"))
# patience=2 (consecutive frames) is the shippable operating point for "hey
# hermes": 0.9/2 -> 5.4 FP/hr at 95.6% recall on held-out noise.
PATIENCE = int(os.getenv("WAKEWORD_PATIENCE", "2"))
# Per-word overrides, e.g. WAKEWORD_THRESHOLDS=hermes:0.95 (single short words are
# FP-prone, so they typically need a higher threshold/patience than a phrase).
WAKEWORD_THRESHOLDS = os.getenv("WAKEWORD_THRESHOLDS", "")
WAKEWORD_PATIENCES = os.getenv("WAKEWORD_PATIENCES", "")
# Explicit per-word verifier overrides, e.g. hermes:/app/models/hermes_verifier.npz.
WAKEWORD_VERIFIERS = os.getenv("WAKEWORD_VERIFIERS", "")
_vt = os.getenv("WAKEWORD_VERIFIER_THRESHOLD", "")
VERIFIER_THRESHOLD = float(_vt) if _vt else None

DEBOUNCE_SECS = float(os.getenv("WAKEWORD_DEBOUNCE_SECS", "3.0"))
VAD_THRESHOLD = float(os.getenv("WAKEWORD_VAD_THRESHOLD", "0.5"))
# End-of-turn is decided by the Smart Turn MODEL at each speech->silence pause
# (eot_min_silence -> first query, eot_recheck -> re-query cadence). stop_secs is
# now only a LONG backstop for when the model never fires (pure trailing silence).
STOP_SECS = float(os.getenv("WAKEWORD_STOP_SECS", "6.0"))
EOT_MIN_SILENCE_SECS = float(os.getenv("WAKEWORD_EOT_MIN_SILENCE_SECS", "0.2"))
EOT_RECHECK_SECS = float(os.getenv("WAKEWORD_EOT_RECHECK_SECS", "0.3"))
MAX_ARM_SECS = float(os.getenv("WAKEWORD_MAX_ARM_SECS", "15.0"))
# Minimum spoken (VAD) speech a captured command must contain to be sent for
# batch ASR. Below this the capture is a near-silent false arm; the backend skips
# transcription so self-diarizing ASR can't hallucinate a phantom command.
MIN_COMMAND_SPEECH_SECS = float(os.getenv("WAKEWORD_MIN_COMMAND_SPEECH_SECS", "0.3"))
SERVICE_PORT = int(os.getenv("WAKEWORD_SERVICE_PORT", "8770"))
# Root for the data-collection clip store (mounted volume in docker-compose).
DATA_DIR = os.getenv("WAKEWORD_DATA_DIR", "/app/data/samples")
# "Prime + say it" capture tuning (data collection). Hard upper bound of 10 s
# on the whole priming session; the utterance itself caps at PRIME_MAX_SECS and
# normally ends a beat after a trailing-silence gap (so capture lands in ~5 s).
PRIME_TIMEOUT_SECS = float(os.getenv("WAKEWORD_PRIME_TIMEOUT_SECS", "10.0"))
PRIME_TRAIL_SILENCE_SECS = float(os.getenv("WAKEWORD_PRIME_TRAIL_SILENCE_SECS", "0.6"))
PRIME_MAX_SECS = float(os.getenv("WAKEWORD_PRIME_MAX_SECS", "4.0"))
# VAD gate while priming — dropped well below the live VAD threshold so the
# known-incoming utterance is auto-caught even when spoken softly.
PRIME_VAD_THRESHOLD = float(os.getenv("WAKEWORD_PRIME_VAD_THRESHOLD", "0.3"))
# Optional explicit ONNX paths (vendored into models/). Empty -> pipecat bundle.
SMART_TURN_MODEL_PATH = os.getenv("SMART_TURN_MODEL_PATH", "") or None
SILERO_VAD_MODEL_PATH = os.getenv("SILERO_VAD_MODEL_PATH", "") or None


def _parse_models(spec: str) -> dict[str, str]:
    """Parse ``WAKEWORD_MODELS`` into an ordered ``{name: abs_model_path}``."""
    models: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, file = (p.strip() for p in item.split(":", 1))
        else:
            file = item
            name = os.path.splitext(os.path.basename(file))[0]
        path = file if os.path.isabs(file) else os.path.join(MODELS_DIR, file)
        models[name] = path
    return models


def _parse_kv(spec: str, cast) -> dict:
    """Parse a ``name:value,name:value`` spec into a typed dict."""
    out: dict = {}
    for item in spec.split(","):
        item = item.strip()
        if ":" in item:
            k, v = (p.strip() for p in item.split(":", 1))
            out[k] = cast(v)
    return out


MODELS = _parse_models(WAKEWORD_MODELS)
WAKEWORDS = list(MODELS.keys())
THRESHOLDS = _parse_kv(WAKEWORD_THRESHOLDS, float)
PATIENCES = _parse_kv(WAKEWORD_PATIENCES, int)
COLLECT_ONLY = [w.strip() for w in WAKEWORD_COLLECT_ONLY.split(",") if w.strip()]


def _resolve_verifiers() -> dict[str, str]:
    """Per-word verifier paths: explicit override, else ``<name>_verifier.npz`` if
    it exists. Words with no verifier file (and no override) get no entry."""
    overrides = _parse_kv(WAKEWORD_VERIFIERS, str)
    out: dict[str, str] = {}
    for name in WAKEWORDS:
        if name in overrides:
            out[name] = overrides[name]
            continue
        default = os.path.join(MODELS_DIR, f"{name}_verifier.npz")
        if os.path.exists(default):
            out[name] = default
    return out


VERIFIERS = _resolve_verifiers()


def _validate_models() -> None:
    """Refuse to start unless every configured wake model file exists on disk."""
    available = (
        sorted(f for f in os.listdir(MODELS_DIR) if f.endswith(".onnx"))
        if os.path.isdir(MODELS_DIR)
        else []
    )
    required = dict(MODELS)
    if SMART_TURN_MODEL_PATH:
        required["smart-turn"] = SMART_TURN_MODEL_PATH
    if SILERO_VAD_MODEL_PATH:
        required["silero-vad"] = SILERO_VAD_MODEL_PATH
    missing = {name: p for name, p in required.items() if not os.path.exists(p)}
    if missing:
        details = "; ".join(f"{name} '{p}'" for name, p in missing.items())
        raise FileNotFoundError(
            f"Configured model(s) not found on disk: {details}. "
            f"Available .onnx in {MODELS_DIR}: {available or '(none)'}. "
            f"Train/vendor the model and set WAKEWORD_MODELS accordingly."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the detector and start the background consumer."""
    if not MODELS:
        raise RuntimeError("WAKEWORD_MODELS is empty — configure at least one word")
    _validate_models()

    detector = HermesDetector(
        models=MODELS,
        threshold=THRESHOLD,
        patience=PATIENCE,
        thresholds=THRESHOLDS,
        patiences=PATIENCES,
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
        prime_vad_threshold=PRIME_VAD_THRESHOLD,
        min_command_speech_secs=MIN_COMMAND_SPEECH_SECS,
        verifiers=VERIFIERS,
        verifier_threshold=VERIFIER_THRESHOLD,
        collect_only=COLLECT_ONLY,
    )
    store = SampleStore(DATA_DIR, WAKEWORDS, legacy_wakeword=LEGACY_WAKEWORD)
    app.state.store = store
    consumer = WakeWordConsumer(
        detector=detector, redis_url=REDIS_URL, sample_store=store
    )
    app.state.consumer = consumer
    consumer_task = asyncio.create_task(consumer.start())
    app.state.consumer_task = consumer_task
    logger.info(
        f"Wake-word service ready (words={', '.join(WAKEWORDS)}, group={GROUP_NAME})"
    )

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


def _wakeword_summaries() -> list[dict]:
    """Per-word config summary for the dashboard."""
    return [
        {
            "name": name,
            "model": os.path.basename(MODELS[name]),
            "verifier": name in VERIFIERS and os.path.exists(VERIFIERS[name]),
            "threshold": THRESHOLDS.get(name, THRESHOLD),
            "patience": PATIENCES.get(name, PATIENCE),
            "collect_only": name in COLLECT_ONLY,
        }
        for name in WAKEWORDS
    ]


@app.get("/health")
async def health(response: Response):
    """Liveness of the actual work loop, not just the HTTP server.

    The consumer runs as a background task. If it dies (e.g. an unrecoverable
    error in the discovery loop), uvicorn keeps serving requests — so reporting
    "ok" purely on the HTTP server being up would mask a service that no longer
    consumes any audio. Reflect the real consumer state and return 503 when it
    is not alive, so orchestrators / the dashboard see the failure.
    """
    consumer: WakeWordConsumer | None = getattr(app.state, "consumer", None)
    task: asyncio.Task | None = getattr(app.state, "consumer_task", None)
    consumer_alive = bool(
        consumer is not None
        and consumer.running
        and task is not None
        and not task.done()
    )
    status_str = "ok" if consumer_alive else "unhealthy"
    if not consumer_alive:
        response.status_code = 503
    return {
        "status": status_str,
        "consumer_alive": consumer_alive,
        "wakewords": _wakeword_summaries(),
        "redis_url": REDIS_URL,
        "consumer_group": GROUP_NAME,
        "detections_stream": DETECTIONS_STREAM,
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


@app.get("/models")
async def models():
    """Configured wake words (for the dashboard picker + the Wake-Word Lab).

    ``available``/``active`` are kept for the existing acoustic-condition picker
    (now the list of wake-word NAMES); ``wakewords`` carries the richer per-word
    config the Lab uses to render one section per word.
    """
    return {
        "available": WAKEWORDS,
        "active": WAKEWORDS[0] if WAKEWORDS else None,
        "wakewords": _wakeword_summaries(),
    }


# --------------------------------------------------------------------------- #
# Data-collection API (the training flywheel): prime positive capture, review
# captured clips, label them true/false positive — all scoped per wake word.
# Consumed by the backend proxy at /api/wakeword/* — see backends/advanced
# .../routers/modules/wakeword_routes.py
# --------------------------------------------------------------------------- #


class PrimeRequest(BaseModel):
    client_id: str
    wakeword: str


class UnprimeRequest(BaseModel):
    client_id: str


class LabelRequest(BaseModel):
    label: str  # "wake" -> positive, "not_wake" -> negative


def _require_wakeword(wakeword: str) -> None:
    if wakeword not in MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown wake word '{wakeword}' (have {WAKEWORDS})",
        )


@app.get("/streams")
async def streams():
    """Active audio streams the UI can prime for a positive capture."""
    consumer: WakeWordConsumer = app.state.consumer
    return {"streams": consumer.active_clients()}


@app.post("/prime")
async def prime(req: PrimeRequest):
    """Arm a one-shot positive capture of ``wakeword`` on a streaming client.

    The next utterance on that stream is saved as a labeled positive for that
    word regardless of model score (the false-negative / hard-positive path).
    """
    _require_wakeword(req.wakeword)
    consumer: WakeWordConsumer = app.state.consumer
    if not consumer.prime(req.client_id, req.wakeword):
        raise HTTPException(
            status_code=404,
            detail=f"No active stream '{req.client_id}' to prime",
        )
    return {"client_id": req.client_id, "wakeword": req.wakeword, "primed": True}


@app.post("/unprime")
async def unprime(req: UnprimeRequest):
    """Manually end an in-progress prime capture (the UI 'stop' button).

    Finalizes whatever was heard so far and saves it for review — the capture is
    never silently dropped.
    """
    consumer: WakeWordConsumer = app.state.consumer
    if not consumer.unprime(req.client_id):
        raise HTTPException(
            status_code=404,
            detail=f"No priming stream '{req.client_id}' to stop",
        )
    return {"client_id": req.client_id, "unprimed": True}


@app.get("/samples")
async def list_samples(wakeword: str = Query(...), bucket: str = Query("pending")):
    """List captured clips in a wake word's bucket (pending/positive/negative)."""
    _require_wakeword(wakeword)
    if bucket not in BUCKETS:
        raise HTTPException(status_code=400, detail=f"bad bucket (expected {BUCKETS})")
    store: SampleStore = app.state.store
    return {
        "wakeword": wakeword,
        "bucket": bucket,
        "samples": store.list(wakeword, bucket),
    }


@app.get("/samples/stats")
async def sample_stats():
    """Per-wake-word, per-bucket clip counts for the data-collection dashboard."""
    store: SampleStore = app.state.store
    return store.stats()


@app.post("/samples/dedupe")
async def dedupe_samples(wakeword: str = Query(...)):
    """Remove exact-duplicate clips within a wake word (across all buckets).

    Keeps one representative per identical-audio group (a labeled clip over a
    pending one), deleting the rest — including duplicate pending clips.
    """
    _require_wakeword(wakeword)
    store: SampleStore = app.state.store
    return store.dedupe(wakeword)


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


@app.post("/samples/{clip_id}/move")
async def move_sample(
    clip_id: str, wakeword: str = Query(...), bucket: str = Query(PENDING)
):
    """Move a clip to a different wake word's bucket (default pending).

    For the acoustic-overlap case: a live arm attributed to the priority word that
    is really another word's utterance (e.g. a bare "hermes" that landed in
    hey_hermes). Harvests it into the right word as real-usage training data.
    """
    _require_wakeword(wakeword)
    if bucket not in BUCKETS:
        raise HTTPException(status_code=400, detail=f"bad bucket (expected {BUCKETS})")
    store: SampleStore = app.state.store
    try:
        return store.move(clip_id, wakeword, bucket)
    except KeyError:
        raise HTTPException(status_code=404, detail="clip not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/samples/{clip_id}/copy")
async def copy_sample(
    clip_id: str, wakeword: str = Query(...), bucket: str = Query(PENDING)
):
    """Copy a clip into another wake word's bucket (source stays).

    For a false positive that fired several words — it's a hard negative for each,
    so fan it out instead of moving it.
    """
    _require_wakeword(wakeword)
    if bucket not in BUCKETS:
        raise HTTPException(status_code=400, detail=f"bad bucket (expected {BUCKETS})")
    store: SampleStore = app.state.store
    try:
        return store.copy(clip_id, wakeword, bucket)
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
