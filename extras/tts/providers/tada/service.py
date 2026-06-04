"""
TADA TTS Service.

FastAPI service implementation for HumeAI TADA TTS provider.
"""

import argparse
import asyncio
import logging
import os
from typing import Optional

import uvicorn

from common.base_service import BaseTTSService, create_tts_app
from providers.tada.synthesizer import TadaSynthesizer

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class TadaService(BaseTTSService):
    """
    TTS service using HumeAI TADA.

    Supports:
    - HumeAI/tada-1b (English, ~4-5GB VRAM)
    - HumeAI/tada-3b-ml (Multilingual, ~7-8GB VRAM)

    Environment variables:
        TTS_MODEL: Model identifier (default: HumeAI/tada-1b)
        TTS_LANGUAGE: Language code for multilingual model (default: None/English)
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.synthesizer: Optional[TadaSynthesizer] = None

    @property
    def provider_name(self) -> str:
        return "tada"

    async def warmup(self) -> None:
        """Initialize and warm up the model."""
        logger.info(f"Initializing TADA with model: {self.model_id}")

        loop = asyncio.get_event_loop()
        self.synthesizer = TadaSynthesizer(self.model_id)
        await loop.run_in_executor(None, self.synthesizer.load_model)

        # Warm up with short text
        logger.info("Warming up model...")
        try:
            await self.synthesizer.synthesize("Hello.")
            logger.info("Model warmed up successfully")
        except Exception as e:
            logger.warning(f"Warmup failed (non-critical): {e}")

    async def synthesize(
        self,
        text: str,
        reference_audio_path: Optional[str] = None,
        reference_text: Optional[str] = None,
        **kwargs,
    ) -> tuple[bytes, int]:
        """Synthesize speech from text."""
        if self.synthesizer is None:
            raise RuntimeError("Service not initialized")

        return await self.synthesizer.synthesize(
            text=text,
            reference_audio_path=reference_audio_path,
            reference_text=reference_text,
        )

    def get_capabilities(self) -> list[str]:
        capabilities = ["voice_cloning", "speech_continuation"]
        if "3b-ml" in self.model_id:
            capabilities.append("multilingual")
        return capabilities

    def get_supported_languages(self) -> Optional[list[str]]:
        if "3b-ml" in self.model_id:
            return ["en", "ar", "zh", "de", "es", "fr", "it", "ja", "pl", "pt"]
        return ["en"]


def main():
    """Main entry point for TADA TTS service."""
    parser = argparse.ArgumentParser(description="TADA TTS Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8770, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    if args.model:
        os.environ["TTS_MODEL"] = args.model

    model_id = os.getenv("TTS_MODEL", "HumeAI/tada-1b")

    service = TadaService(model_id)
    app = create_tts_app(service)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
