"""
Fish Speech TTS Service.

FastAPI service implementation for Fish Audio's Fish Speech TTS provider.
When running inside the container, startup.py handles model download and
fish-speech server launch before this service starts.
"""

import asyncio
import logging
import os
from typing import Optional

from common.base_service import BaseTTSService
from providers.fish_speech.synthesizer import FishSpeechSynthesizer

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class FishSpeechService(BaseTTSService):
    """
    TTS service using Fish Audio's Fish Speech.

    Supports:
    - fishaudio/s2-pro (default, 83 languages, ~11GB)
    - fishaudio/openaudio-s1-mini (0.5B params, requires tokenizer workarounds)
    - fishaudio/fish-speech-1.5 (larger model, higher quality)

    Environment variables:
        TTS_MODEL: HuggingFace model ID (default: fishaudio/s2-pro)
        TTS_COMPILE: Enable torch.compile for ~10x speedup (default: false)
        TTS_HALF: Use half precision (default: true)
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.synthesizer: Optional[FishSpeechSynthesizer] = None

    @property
    def provider_name(self) -> str:
        return "fish-speech"

    async def warmup(self) -> None:
        """Initialize the HTTP client and detect sample rate.

        The fish-speech server is already running (started by startup.py).
        We just need to verify connectivity and detect the sample rate.
        """
        logger.info(f"Initializing Fish Speech client for model: {self.model_id}")

        loop = asyncio.get_event_loop()
        self.synthesizer = FishSpeechSynthesizer(self.model_id)
        await loop.run_in_executor(None, self.synthesizer.initialize)

        logger.info("Fish Speech client ready")

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
            **kwargs,
        )

    def get_capabilities(self) -> list[str]:
        return [
            "voice_cloning",
            "multilingual",
            "emotion_control",
            "streaming",
        ]

    def get_supported_languages(self) -> Optional[list[str]]:
        # Fish Speech supports 50+ languages, these are the best quality ones
        return [
            "en",
            "ja",
            "zh",
            "ko",
            "fr",
            "de",
            "es",
            "pt",
            "it",
            "ar",
        ]
