"""
NeMo transcriber implementation.

Uses NVIDIA NeMo ASR models (Parakeet, Canary, etc.) with native
timestamp support. NeMo handles long audio internally.
"""

import asyncio
import logging
import os
from typing import Optional, cast

import torch

from common.response_models import TranscriptionResult, Word

logger = logging.getLogger(__name__)

# Constants
NEMO_SAMPLE_RATE = 16000


class NemoTranscriber:
    """
    Transcriber using NVIDIA NeMo ASR models.

    Supports:
    - nvidia/parakeet-tdt-0.6b-v3
    - nvidia/canary-1b
    - Other NeMo ASR models

    NeMo's transcribe() method handles long audio natively with word-level
    timestamps - no custom chunking required.

    Environment variables:
        ASR_MODEL: Model identifier (default: nvidia/parakeet-tdt-0.6b-v3)
    """

    def __init__(self, model_id: Optional[str] = None):
        """
        Initialize the NeMo transcriber.

        Args:
            model_id: Model identifier. If None, reads from ASR_MODEL env var.
        """
        self.model_id = model_id or os.getenv(
            "ASR_MODEL", "nvidia/parakeet-tdt-0.6b-v3"
        )

        self.model = None
        self._is_loaded = False
        self._lock = asyncio.Lock()

        logger.info(f"NemoTranscriber initialized: model={self.model_id}")

    def load_model(self) -> None:
        """Load the NeMo ASR model."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        logger.info(f"Loading NeMo ASR model: {self.model_id}")

        import nemo.collections.asr as nemo_asr

        self.model = cast(
            nemo_asr.models.ASRModel,
            nemo_asr.models.ASRModel.from_pretrained(model_name=self.model_id),
        )

        self._is_loaded = True
        logger.info("Model loaded successfully")

    async def transcribe(self, audio_file_path: str) -> TranscriptionResult:
        """
        Transcribe audio file using NeMo.

        NeMo's transcribe() handles long audio natively with timestamps=True.
        No custom chunking is needed.

        Args:
            audio_file_path: Path to audio file (WAV format, 16kHz mono preferred)

        Returns:
            TranscriptionResult with text, words, and segments
        """
        if not self._is_loaded or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        logger.info(f"Transcribing: {audio_file_path}")

        async with self._lock:
            with torch.no_grad():
                results = self.model.transcribe(
                    [audio_file_path], batch_size=1, timestamps=True
                )

        if not results or len(results) == 0:
            logger.warning("NeMo returned empty results")
            return TranscriptionResult(text="", words=[], segments=[])

        result = results[0]

        # Extract text
        if hasattr(result, "text") and result.text:
            text = result.text
        elif isinstance(result, str):
            text = result
        else:
            text = ""

        # Extract word-level timestamps - NeMo Parakeet format
        words = []
        if hasattr(result, "timestamp") and "word" in result.timestamp:
            for word_data in result.timestamp["word"]:
                word = Word(
                    word=word_data["word"],
                    start=word_data["start"],
                    end=word_data["end"],
                    confidence=1.0,
                )
                words.append(word)

        logger.info(f"Transcription complete: {len(text)} chars, {len(words)} words")

        return TranscriptionResult(
            text=text,
            words=words,
            segments=[],
        )

    @property
    def is_loaded(self) -> bool:
        """Return True if model is loaded."""
        return self._is_loaded
