"""
Audio Flamingo Next ASR Service.

FastAPI service for NVIDIA Audio Flamingo Next: prompt-driven speech
transcription with timestamped multi-talker diarization. License is
NVIDIA OneWay Noncommercial — research use only.
"""

import argparse
import asyncio
import logging
import os
from typing import Optional

import uvicorn
from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult
from providers.af_next.transcriber import AudioFlamingoNextTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class AudioFlamingoNextService(BaseASRService):
    """ASR service using NVIDIA Audio Flamingo Next."""

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.transcriber: Optional[AudioFlamingoNextTranscriber] = None

    @property
    def provider_name(self) -> str:
        return "af_next"

    async def warmup(self) -> None:
        logger.info(f"Initializing Audio Flamingo Next with model: {self.model_id}")
        loop = asyncio.get_event_loop()
        self.transcriber = AudioFlamingoNextTranscriber(self.model_id)
        await loop.run_in_executor(None, self.transcriber.load_model)
        logger.info("Audio Flamingo Next model loaded and ready")

    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> TranscriptionResult:
        transcriber = self.transcriber
        if transcriber is None:
            raise RuntimeError("Service not initialized")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: transcriber.transcribe(
                audio_file_path,
                context_info=context_info,
                prompt_override=prompt,
            ),
        )
        return result

    def get_capabilities(self) -> list[str]:
        return ["timestamps", "diarization"]


def main():
    parser = argparse.ArgumentParser(description="Audio Flamingo Next ASR Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    if args.model:
        os.environ["ASR_MODEL"] = args.model

    model_id = os.getenv("ASR_MODEL", "nvidia/audio-flamingo-next-think-hf")
    service = AudioFlamingoNextService(model_id)
    app = create_asr_app(service)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
