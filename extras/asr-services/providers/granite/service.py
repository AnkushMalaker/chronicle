"""
IBM Granite Speech ASR Service.

FastAPI service wrapping IBM's Granite Speech multimodal model for batch
transcription. A batch-only provider (no incremental/streaming decoding), it
serves the standard ``/transcribe``, ``/health`` and ``/info`` endpoints via the
shared ``create_asr_app`` factory.
"""

import argparse
import asyncio
import logging
import os
from typing import Optional

import uvicorn
from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult
from providers.granite.transcriber import DEFAULT_MODEL, GraniteTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class GraniteService(BaseASRService):
    """ASR service using IBM Granite Speech multimodal model."""

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.transcriber: Optional[GraniteTranscriber] = None

    @property
    def provider_name(self) -> str:
        return "granite"

    async def warmup(self) -> None:
        logger.info(f"Initializing Granite Speech with model: {self.model_id}")
        loop = asyncio.get_event_loop()
        self.transcriber = GraniteTranscriber(self.model_id)
        await loop.run_in_executor(None, self.transcriber.load_model)
        logger.info("Granite Speech model loaded and ready")

    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> TranscriptionResult:
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.transcriber.transcribe(
                audio_file_path,
                context_info=context_info,
                prompt_override=prompt,
            ),
        )
        return result

    def supports_batch_progress(self, audio_duration: float) -> bool:
        """Stream NDJSON progress for audio long enough to be batched.

        Without this, long audio is transcribed on the single-JSON path and the
        HTTP response is withheld until every window finishes — which exceeds
        the client's read timeout and fails the request mid-transcription.
        """
        if self.transcriber is None:
            return False
        return self.transcriber.supports_batch_progress(audio_duration)

    def transcribe_with_progress(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
        prompt: Optional[str] = None,
        **kwargs,
    ):
        """Yield progress counters then the final result for long audio.

        Delegates to the transcriber's batched generator (run synchronously via
        run_in_executor by the endpoint) and converts the final result object
        to the NDJSON wire shape.
        """
        if kwargs:
            logger.warning(
                f"transcribe_with_progress: ignoring unsupported kwargs: {list(kwargs.keys())}"
            )
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")
        for event in self.transcriber._transcribe_batched_with_progress(
            audio_file_path,
            context_info=context_info,
            prompt_override=prompt,
        ):
            if event["type"] == "result":
                yield {"type": "result", **event["result"].to_dict()}
            else:
                yield event

    def get_supported_languages(self) -> Optional[list[str]]:
        # Granite Speech 4.1 supports English, French, German, Spanish, Portuguese.
        return ["en", "fr", "de", "es", "pt"]

    def get_capabilities(self) -> list[str]:
        # "diarization" + "segments": Granite emits speaker-attributed segments with
        # real (model-derived) timestamps via its two-pass diarized path, so the
        # backend uses these segments for speaker turns and skips pyannote
        # diarization (running only speaker identification). Disable with
        # GRANITE_DIARIZE=0 if you only want raw transcription.
        #
        # "context_prompt": Granite is an LLM-backbone ASR and consumes context as
        # prompt text (not an acoustic keyword-boost), so the backend feeds it the
        # user-authored asr_context only and withholds the wake-word boost list,
        # which it would otherwise echo into the transcript.
        caps = ["llm", "context_prompt"]
        if self.transcriber is not None and self.transcriber.diarize:
            caps += ["diarization", "segments"]
        return caps


def main():
    parser = argparse.ArgumentParser(description="Granite Speech ASR Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    if args.model:
        os.environ["ASR_MODEL"] = args.model

    model_id = os.getenv("ASR_MODEL", DEFAULT_MODEL)
    service = GraniteService(model_id)
    app = create_asr_app(service)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
