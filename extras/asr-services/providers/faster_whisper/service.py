"""
Faster-Whisper ASR Service.

FastAPI service implementation for faster-whisper provider.
"""

import argparse
import asyncio
import logging
import os
import tempfile
from typing import Optional

import uvicorn

from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult
from providers.faster_whisper.transcriber import FasterWhisperTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class FasterWhisperService(BaseASRService):
    """
    ASR service using faster-whisper (CTranslate2 backend).

    Environment variables:
        ASR_MODEL: Model identifier (default: Systran/faster-whisper-large-v3)
        COMPUTE_TYPE: Quantization type (default: float16)
        DEVICE: Device to use (default: cuda)
        VAD_FILTER: Enable VAD filtering (default: true)
        LANGUAGE: Force language code (default: None for auto-detect)
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.transcriber: Optional[FasterWhisperTranscriber] = None

        # Configuration from environment
        self.vad_filter = os.getenv("VAD_FILTER", "true").lower() == "true"
        self.language = os.getenv("LANGUAGE", None)

    @property
    def provider_name(self) -> str:
        return "faster-whisper"

    async def warmup(self) -> None:
        """Initialize and warm up the model."""
        logger.info(f"Initializing faster-whisper with model: {self.model_id}")

        # Load model (runs in thread pool to not block)
        loop = asyncio.get_event_loop()
        self.transcriber = FasterWhisperTranscriber(self.model_id)
        await loop.run_in_executor(None, self.transcriber.load_model)

        # Warm up with short audio
        logger.info("Warming up model...")
        try:
            import numpy as np
            from common.audio_utils import save_to_temp_wav

            # Create 0.1s silence for warmup
            silence = np.zeros(1600, dtype=np.float32)  # 0.1s at 16kHz
            tmp_path = save_to_temp_wav(silence)

            try:
                await loop.run_in_executor(
                    None,
                    lambda: self.transcriber.transcribe(
                        tmp_path, word_timestamps=False, vad_filter=False
                    ),
                )
            finally:
                os.unlink(tmp_path)

            logger.info("Model warmed up successfully")
        except Exception as e:
            logger.warning(f"Warmup failed (non-critical): {e}")

    async def transcribe(self, audio_file_path: str) -> TranscriptionResult:
        """Transcribe audio file."""
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.transcriber.transcribe(
                audio_file_path,
                language=self.language,
                word_timestamps=True,
                vad_filter=self.vad_filter,
            ),
        )
        return result

    def get_capabilities(self) -> list[str]:
        return [
            "timestamps",
            "word_timestamps",
            "language_detection",
            "vad_filter",
            "translation",
        ]


def main():
    """Main entry point for faster-whisper service."""
    parser = argparse.ArgumentParser(description="Faster-Whisper ASR Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    # Set model via environment if provided
    if args.model:
        os.environ["ASR_MODEL"] = args.model

    # Get model ID
    model_id = os.getenv("ASR_MODEL", "Systran/faster-whisper-large-v3")

    # Create service and app
    service = FasterWhisperService(model_id)
    app = create_asr_app(service)

    # Run server
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
