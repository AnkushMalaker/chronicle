#!/usr/bin/env python3
"""
Mock ASR Server - Mimics ASR provider API for protocol testing.

This server simulates the ASR service endpoints (health, info, transcribe)
used by Chronicle's offline transcription services. It follows the same
patterns as mock_streaming_stt_server.py for consistency.

Architecture:
- FastAPI HTTP server on 0.0.0.0:8765
- Implements /health, /info, /transcribe endpoints
- Returns mock transcription responses for file uploads
- No GPU required - suitable for CI/CD testing

Endpoints:
- GET /health -> {"status": "healthy", "model": "...", "provider": "..."}
- GET /info -> {"model_id": "...", "provider": "...", "capabilities": [...]}
- POST /transcribe -> {"text": "...", "words": [...], "segments": [...]}
"""

import argparse
import logging
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mock ASR Server", version="1.0.0")

# Provider mode determines what capabilities are reported
# Set via MOCK_ASR_PROVIDER env var or --provider CLI arg
PROVIDER_MODE = os.environ.get("MOCK_ASR_PROVIDER", "parakeet")


# Response models
class HealthResponse(BaseModel):
    status: str
    model: str
    provider: str


class InfoResponse(BaseModel):
    model_id: str
    provider: str
    capabilities: list[str]
    supported_languages: Optional[list[str]] = None


class WordTimestamp(BaseModel):
    word: str
    start: float
    end: float
    confidence: float


class TranscriptionResult(BaseModel):
    text: str
    words: list[WordTimestamp]
    segments: list[dict]


# Provider configurations - different capabilities per provider type
PROVIDER_CONFIGS = {
    "parakeet": {
        "model_id": "nvidia/parakeet-tdt-0.6b-v3",
        "provider": "parakeet",
        "capabilities": ["timestamps", "word_timestamps", "segments"],
        "has_diarization": False,
        "has_word_timestamps": True,
    },
    "vibevoice": {
        "model_id": "microsoft/VibeVoice-ASR",
        "provider": "vibevoice",
        "capabilities": ["segments", "diarization", "timestamps"],
        "has_diarization": True,
        "has_word_timestamps": False,  # VibeVoice provides segments but no word-level timestamps
    },
    "deepgram": {
        "model_id": "nova-2",
        "provider": "deepgram",
        "capabilities": ["timestamps", "word_timestamps", "segments", "diarization"],
        "has_diarization": True,
        "has_word_timestamps": True,
    },
    "mock": {
        "model_id": "mock-model-v1",
        "provider": "mock",
        "capabilities": ["timestamps", "word_timestamps"],
        "has_diarization": False,
        "has_word_timestamps": True,
    },
}


def get_provider_config():
    """Get configuration for the current provider mode."""
    return PROVIDER_CONFIGS.get(PROVIDER_MODE, PROVIDER_CONFIGS["mock"])


# Mock transcription data
MOCK_TRANSCRIPT = "This is a mock transcription for testing purposes"
MOCK_WORDS = [
    {"word": "This", "start": 0.0, "end": 0.2, "confidence": 0.99},
    {"word": "is", "start": 0.25, "end": 0.35, "confidence": 0.99},
    {"word": "a", "start": 0.4, "end": 0.45, "confidence": 0.98},
    {"word": "mock", "start": 0.5, "end": 0.7, "confidence": 0.99},
    {"word": "transcription", "start": 0.75, "end": 1.2, "confidence": 0.97},
    {"word": "for", "start": 1.25, "end": 1.4, "confidence": 0.99},
    {"word": "testing", "start": 1.45, "end": 1.75, "confidence": 0.98},
    {"word": "purposes", "start": 1.8, "end": 2.2, "confidence": 0.96},
]

# Mock diarized segments (for providers with built-in diarization)
MOCK_DIARIZED_SEGMENTS = [
    {
        "speaker": "Speaker 0",
        "start": 0.0,
        "end": 1.2,
        "text": "This is a mock transcription",
    },
    {"speaker": "Speaker 1", "start": 1.25, "end": 2.2, "text": "for testing purposes"},
]


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    config = get_provider_config()
    logger.debug(f"Health check requested (provider: {config['provider']})")
    return HealthResponse(
        status="healthy", model=config["model_id"], provider=config["provider"]
    )


@app.get("/info", response_model=InfoResponse)
async def info():
    """Service info endpoint."""
    config = get_provider_config()
    logger.debug(f"Info requested (provider: {config['provider']})")
    return InfoResponse(
        model_id=config["model_id"],
        provider=config["provider"],
        capabilities=config["capabilities"],
        supported_languages=None,
    )


@app.post("/transcribe", response_model=TranscriptionResult)
async def transcribe(file: UploadFile = File(...)):
    """
    Transcribe an uploaded audio file.

    In mock mode, returns a fixed transcription regardless of input.
    Response varies based on provider mode:
    - parakeet/mock: words with timestamps, empty segments
    - vibevoice: segments with speakers (diarization), no word timestamps
    - deepgram: both words and diarized segments
    """
    content = await file.read()
    file_size = len(content)
    config = get_provider_config()

    logger.info(
        f"Received file: {file.filename}, size: {file_size} bytes (provider: {config['provider']})"
    )

    # Basic validation - reject empty files
    if file_size == 0:
        raise HTTPException(status_code=400, detail="Empty file received")

    # Build response based on provider capabilities
    words = []
    segments = []

    if config["has_word_timestamps"]:
        words = [WordTimestamp(**w) for w in MOCK_WORDS]

    if config["has_diarization"]:
        segments = MOCK_DIARIZED_SEGMENTS

    return TranscriptionResult(text=MOCK_TRANSCRIPT, words=words, segments=segments)


def main(host: str, port: int, provider: str = "mock", debug: bool = False):
    """Start the mock ASR server."""
    global PROVIDER_MODE
    PROVIDER_MODE = provider

    log_level = "debug" if debug else "info"
    config = get_provider_config()

    logger.info(f"Starting Mock ASR Server on {host}:{port}")
    logger.info(f"Provider mode: {provider}")
    logger.info(f"Capabilities: {config['capabilities']}")
    logger.info(f"Has diarization: {config['has_diarization']}")
    logger.info(f"Has word timestamps: {config['has_word_timestamps']}")
    logger.info("Endpoints: /health, /info, /transcribe")

    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock ASR Server")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Server port (default: 8765)"
    )
    parser.add_argument(
        "--provider",
        default="mock",
        choices=["mock", "parakeet", "vibevoice", "deepgram"],
        help="Provider mode - determines reported capabilities (default: mock)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    main(args.host, args.port, args.provider, args.debug)
