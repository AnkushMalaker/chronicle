"""
VibeVoice ASR Service.

FastAPI service implementation for Microsoft VibeVoice-ASR provider.
"""

import argparse
import asyncio
import logging
import os
from typing import Optional

import uvicorn

from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult
from providers.vibevoice.transcriber import VibeVoiceTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class VibeVoiceService(BaseASRService):
    """
    ASR service using Microsoft VibeVoice-ASR.

    VibeVoice provides speech-to-text with built-in speaker diarization.

    Environment variables:
        ASR_MODEL: Model identifier (default: microsoft/VibeVoice-ASR)
        VIBEVOICE_LLM_MODEL: LLM backbone for processor (default: Qwen/Qwen2.5-7B)
        VIBEVOICE_ATTN_IMPL: Attention implementation (default: sdpa)
        DEVICE: Device to use (default: cuda)
        TORCH_DTYPE: Torch dtype (default: bfloat16)
        MAX_NEW_TOKENS: Max tokens for generation (default: 8192)
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.transcriber: Optional[VibeVoiceTranscriber] = None

    @property
    def provider_name(self) -> str:
        return "vibevoice"

    async def warmup(self) -> None:
        """Initialize and warm up the model."""
        logger.info(f"Initializing VibeVoice with model: {self.model_id}")

        # Load model (runs in thread pool to not block)
        loop = asyncio.get_event_loop()
        self.transcriber = VibeVoiceTranscriber(self.model_id)
        await loop.run_in_executor(None, self.transcriber.load_model)

        # Warmup is skipped for VibeVoice as it's a large model
        # and initial inference can be slow
        logger.info("VibeVoice model loaded and ready")

    async def transcribe(self, audio_file_path: str) -> TranscriptionResult:
        """Transcribe audio file."""
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.transcriber.transcribe(audio_file_path),
        )
        return result

    def get_capabilities(self) -> list[str]:
        return [
            "timestamps",
            "diarization",
            "speaker_identification",
            "long_form",
        ]


def main():
    """Main entry point for VibeVoice service."""
    parser = argparse.ArgumentParser(description="VibeVoice ASR Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    # Set model via environment if provided
    if args.model:
        os.environ["ASR_MODEL"] = args.model

    # Get model ID
    model_id = os.getenv("ASR_MODEL", "microsoft/VibeVoice-ASR")

    # Create service and app
    service = VibeVoiceService(model_id)
    app = create_asr_app(service)

    # Run server
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
