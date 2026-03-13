"""
Qwen3-ASR batch service.

FastAPI service that exposes /transcribe for batch audio transcription
via a vLLM server running Qwen3-ASR.
"""

import argparse
import logging
import os
from typing import Optional

import uvicorn
from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult
from providers.qwen3_asr.transcriber import Qwen3ASRTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class Qwen3ASRService(BaseASRService):
    """ASR service using Qwen3-ASR via vLLM.

    This is a lightweight wrapper — the heavy GPU work is done by the
    separate vLLM container. This service handles multipart file uploads,
    encodes audio, and forwards to vLLM.

    Environment variables:
        ASR_MODEL: Model identifier (default: Qwen/Qwen3-ASR-1.7B)
        QWEN3_VLLM_URL: vLLM server URL (default: http://localhost:8000)
        FORCED_ALIGNER_MODEL: Optional model ID for word-level timestamps
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.transcriber: Optional[Qwen3ASRTranscriber] = None

    @property
    def provider_name(self) -> str:
        return "qwen3-asr"

    async def warmup(self) -> None:
        """Check that the vLLM backend is reachable."""
        logger.info(f"Initializing Qwen3-ASR wrapper for model: {self.model_id}")
        self.transcriber = Qwen3ASRTranscriber(self.model_id)

        healthy = await self.transcriber.check_health()
        if healthy:
            logger.info("vLLM server is healthy — Qwen3-ASR wrapper ready")
        else:
            logger.warning(
                "vLLM server not reachable yet — requests will fail until it is up. "
                "The vLLM container may still be loading the model."
            )

    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe audio file via vLLM."""
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")
        return await self.transcriber.transcribe(
            audio_file_path, context_info=context_info
        )

    def get_capabilities(self) -> list[str]:
        capabilities = ["multilingual", "language_detection"]
        # Dynamically add word_timestamps when ForcedAligner is loaded
        if self.transcriber and self.transcriber._aligner is not None:
            capabilities.append("word_timestamps")
        return capabilities


def main():
    """Main entry point for Qwen3-ASR batch wrapper service."""
    parser = argparse.ArgumentParser(description="Qwen3-ASR Batch Wrapper Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    if args.model:
        os.environ["ASR_MODEL"] = args.model

    model_id = os.getenv("ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")

    service = Qwen3ASRService(model_id)
    app = create_asr_app(service)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
