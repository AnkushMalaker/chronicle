"""
NeMo ASR Service.

FastAPI service implementation for NVIDIA NeMo ASR provider.
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
from providers.nemo.transcriber import NemoTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class NemoService(BaseASRService):
    """
    ASR service using NVIDIA NeMo.

    Supports:
    - nvidia/parakeet-tdt-0.6b-v3
    - nvidia/canary-1b
    - Other NeMo ASR models

    Environment variables:
        ASR_MODEL: Model identifier (default: nvidia/parakeet-tdt-0.6b-v3)
        CHUNKING_ENABLED: Enable chunking for long audio (default: true)
        MIN_AUDIO_FOR_CHUNKING: Minimum duration to use chunking (default: 60.0)
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.transcriber: Optional[NemoTranscriber] = None

    @property
    def provider_name(self) -> str:
        return "nemo"

    async def warmup(self) -> None:
        """Initialize and warm up the model."""
        logger.info(f"Initializing NeMo with model: {self.model_id}")

        # Load model (runs in thread pool to not block)
        loop = asyncio.get_event_loop()
        self.transcriber = NemoTranscriber(self.model_id)
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
                await self.transcriber.transcribe(tmp_path)
            finally:
                os.unlink(tmp_path)

            logger.info("Model warmed up successfully")
        except Exception as e:
            logger.warning(f"Warmup failed (non-critical): {e}")

    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe audio file. context_info is not used by this provider."""
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")

        return await self.transcriber.transcribe(audio_file_path)

    def get_capabilities(self) -> list[str]:
        return [
            "timestamps",
            "word_timestamps",
            "chunked_processing",
        ]


def main():
    """Main entry point for NeMo service."""
    parser = argparse.ArgumentParser(description="NeMo ASR Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    # Set model via environment if provided
    if args.model:
        os.environ["ASR_MODEL"] = args.model

    # Get model ID (support legacy PARAKEET_MODEL env var)
    model_id = os.getenv("ASR_MODEL") or os.getenv(
        "PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v3"
    )

    # Create service and app
    service = NemoService(model_id)
    app = create_asr_app(service)

    # Run server
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
