"""Smart turn-taking detection service.

Uses the pipecat-ai/smart-turn ONNX model to predict whether a speaker's turn
is complete based on intonation and linguistic cues from raw audio.
"""

import logging
import os
from contextlib import asynccontextmanager

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, Request, Response
from transformers import WhisperFeatureExtractor

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_SECONDS = 8
MAX_SAMPLES = CHUNK_SECONDS * SAMPLE_RATE  # 128,000
THRESHOLD = 0.5

MODEL_DIR = os.environ.get("MODEL_DIR", "/app/models")
MODEL_FILENAME = os.environ.get("MODEL_FILENAME", "smart-turn-v3.2-cpu.onnx")

# Globals initialized at startup
feature_extractor: WhisperFeatureExtractor = None
ort_session: ort.InferenceSession = None


def _load_model():
    global feature_extractor, ort_session

    model_path = os.path.join(MODEL_DIR, MODEL_FILENAME)
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model not found at {model_path}")

    feature_extractor = WhisperFeatureExtractor(chunk_length=CHUNK_SECONDS)

    so = ort.SessionOptions()
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    ort_session = ort.InferenceSession(model_path, sess_options=so)

    logger.info("Model loaded from %s", model_path)


def truncate_to_last_n_seconds(audio: np.ndarray) -> np.ndarray:
    """Keep last CHUNK_SECONDS of audio, zero-pad at beginning if shorter."""
    if len(audio) > MAX_SAMPLES:
        return audio[-MAX_SAMPLES:]
    elif len(audio) < MAX_SAMPLES:
        padding = MAX_SAMPLES - len(audio)
        return np.pad(audio, (padding, 0), mode="constant", constant_values=0)
    return audio


def predict_turn(audio_float32: np.ndarray) -> dict:
    """Run turn prediction on float32 audio array (16kHz mono, range [-1, 1])."""
    audio_float32 = truncate_to_last_n_seconds(audio_float32)

    inputs = feature_extractor(
        audio_float32,
        sampling_rate=SAMPLE_RATE,
        return_tensors="np",
        padding="max_length",
        max_length=MAX_SAMPLES,
        truncation=True,
        do_normalize=True,
    )

    input_features = np.expand_dims(inputs["input_features"][0], axis=0).astype(
        np.float32
    )
    outputs = ort_session.run(None, {"input_features": input_features})
    probability = float(outputs[0][0].item())
    prediction = 1 if probability >= THRESHOLD else 0

    return {"prediction": prediction, "probability": round(probability, 4)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_model()
    yield


app = FastAPI(title="Smart Turn Service", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_FILENAME.replace(".onnx", "")}


@app.post("/predict")
async def predict(request: Request):
    """Accept raw PCM audio (int16, 16kHz, mono) in request body.

    Returns {"prediction": 0|1, "probability": float}
      - 1 = turn complete (speaker finished)
      - 0 = turn incomplete (speaker still going)
    """
    audio_bytes = await request.body()
    if len(audio_bytes) < 2:
        return Response(
            status_code=400, content="Request body must contain PCM int16 audio data"
        )

    audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
    audio_float32 = audio_int16.astype(np.float32) / 32768.0

    result = predict_turn(audio_float32)
    return result


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8766"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")
