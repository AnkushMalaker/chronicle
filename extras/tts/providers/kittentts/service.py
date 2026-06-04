"""
KittenTTS TTS Service.

FastAPI service for the KittenTTS lightweight CPU ONNX provider. Exposes the
standard /health, /info, and /synthesize endpoints via the shared app factory.
"""

import argparse
import asyncio
import logging
import os
from typing import Optional

import uvicorn

from common.base_service import BaseTTSService, create_tts_app
from providers.kittentts.synthesizer import KittenSynthesizer

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class KittenService(BaseTTSService):
    """
    TTS service using KittenTTS — ultra-light (~25MB), CPU-only, no API key.

    Environment variables:
        TTS_MODEL: Model identifier (default: KittenML/kitten-tts-nano-0.8-int8)
        TTS_VOICE: Preset voice name (default: Jasper)
        TTS_SPEED: Speech speed multiplier (default: 1.0)
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.synthesizer: Optional[KittenSynthesizer] = None

    @property
    def provider_name(self) -> str:
        return "kittentts"

    async def warmup(self) -> None:
        """Load the model and run a short warmup synthesis."""
        logger.info(f"Initializing KittenTTS with model: {self.model_id}")

        loop = asyncio.get_event_loop()
        self.synthesizer = KittenSynthesizer(self.model_id)
        await loop.run_in_executor(None, self.synthesizer.load_model)

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
        if self.synthesizer is None:
            raise RuntimeError("Service not initialized")

        return await self.synthesizer.synthesize(
            text=text,
            reference_audio_path=reference_audio_path,
            reference_text=reference_text,
            **kwargs,
        )

    def get_capabilities(self) -> list[str]:
        return ["lightweight", "cpu", "preset_voices"]

    def get_supported_languages(self) -> Optional[list[str]]:
        return ["en"]


def main():
    """Main entry point for the KittenTTS service."""
    parser = argparse.ArgumentParser(description="KittenTTS Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8770, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    if args.model:
        os.environ["TTS_MODEL"] = args.model

    model_id = os.getenv("TTS_MODEL", "KittenML/kitten-tts-nano-0.8-int8")

    service = KittenService(model_id)
    app = create_tts_app(service)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
