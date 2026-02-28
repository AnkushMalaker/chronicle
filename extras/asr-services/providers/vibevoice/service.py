"""
VibeVoice ASR Service.

FastAPI service implementation for Microsoft VibeVoice-ASR provider.
Includes LoRA fine-tuning endpoints for model adaptation from user corrections.
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import uvicorn
from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult
from fastapi import File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from providers.vibevoice.transcriber import VibeVoiceTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Fine-tuning state (module-level singleton)
_finetune_state = {
    "status": "idle",  # idle | training | completed | failed
    "job_id": None,
    "progress": None,
    "error": None,
    "last_completed_job_id": None,
}

_finetune_executor = ThreadPoolExecutor(max_workers=1)


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

    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe audio file."""
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.transcriber.transcribe(
                audio_file_path,
                context_info=context_info,
            ),
        )
        return result

    def get_capabilities(self) -> list[str]:
        return [
            "timestamps",
            "diarization",
            "speaker_identification",
            "long_form",
        ]

    def supports_batch_progress(self, audio_duration: float) -> bool:
        """Return True if audio is long enough for batched transcription with progress."""
        if self.transcriber is None:
            return False
        return self.transcriber.supports_batch_progress(audio_duration)

    def transcribe_with_progress(self, audio_file_path: str, context_info=None):
        """Yield progress counters then final result for long audio.

        Delegates to the transcriber's _transcribe_batched_with_progress() generator.
        Runs synchronously (called via run_in_executor by the endpoint).
        """
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")
        yield from self.transcriber._transcribe_batched_with_progress(
            audio_file_path,
            hotwords=context_info,
        )


def _run_lora_training(
    data_dir: str,
    adapter_output_dir: str,
    lora_r: int,
    lora_alpha: int,
    num_epochs: int,
    job_id: str,
) -> None:
    """Run LoRA fine-tuning in a background thread.

    Imports VibeVoice's finetuning-asr/lora_finetune.py and calls its train()
    function programmatically. On completion, saves the adapter and updates state.
    """
    global _finetune_state
    try:
        _finetune_state["status"] = "training"
        _finetune_state["progress"] = "starting"

        # Import VibeVoice's LoRA fine-tuning module
        import sys

        hf_home = Path(os.getenv("HF_HOME", "/models"))
        vibevoice_dir = hf_home / "vibevoice"
        if str(vibevoice_dir) not in sys.path:
            sys.path.insert(0, str(vibevoice_dir))

        finetune_script = vibevoice_dir / "finetuning-asr" / "lora_finetune.py"
        if not finetune_script.exists():
            raise FileNotFoundError(
                f"VibeVoice LoRA fine-tuning script not found at {finetune_script}"
            )

        # Use importlib to load the script as a module
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "lora_finetune", str(finetune_script)
        )
        lora_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lora_module)

        _finetune_state["progress"] = "training"

        # Call the training function
        lora_module.train(
            data_dir=data_dir,
            output_dir=adapter_output_dir,
            lora_r=lora_r,
            lora_alpha=lora_alpha,
            num_epochs=num_epochs,
        )

        _finetune_state["status"] = "completed"
        _finetune_state["progress"] = "done"
        _finetune_state["last_completed_job_id"] = job_id
        logger.info(
            f"LoRA fine-tuning completed: job_id={job_id}, adapter at {adapter_output_dir}"
        )

    except Exception as e:
        _finetune_state["status"] = "failed"
        _finetune_state["error"] = str(e)
        logger.error(
            f"LoRA fine-tuning failed: job_id={job_id}, error={e}", exc_info=True
        )


def add_finetune_routes(app, service: VibeVoiceService) -> None:
    """Add LoRA fine-tuning endpoints to the VibeVoice FastAPI app."""

    finetune_data_dir = Path(os.getenv("FINETUNE_DATA_DIR", "/models/finetune_data"))
    adapter_base_dir = Path(os.getenv("LORA_ADAPTER_DIR", "/models/lora_adapters"))

    @app.post("/fine-tune")
    async def start_finetune(
        audio_files: list[UploadFile] = File(...),
        labels: str = Form(...),
        lora_r: int = Form(16),
        lora_alpha: int = Form(32),
        num_epochs: int = Form(3),
    ):
        """Start LoRA fine-tuning with uploaded audio files and labels.

        Accepts multipart: audio files + JSON labels string.
        Training runs in a background thread; returns immediately with job_id.
        """
        if _finetune_state["status"] == "training":
            raise HTTPException(status_code=409, detail="Training already in progress")

        if not service.is_ready:
            raise HTTPException(status_code=503, detail="Service not ready")

        try:
            label_data = json.loads(labels)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid labels JSON: {e}")

        job_id = str(uuid.uuid4())[:8]
        job_dir = finetune_data_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        # Save uploaded audio files
        for audio_file in audio_files:
            file_path = job_dir / audio_file.filename
            content = await audio_file.read()
            file_path.write_bytes(content)

        # Save labels JSON
        labels_path = job_dir / "labels.json"
        labels_path.write_text(json.dumps(label_data, indent=2))

        # Output adapter directory
        adapter_output_dir = str(adapter_base_dir / "latest")

        # Update state and launch training in background thread
        _finetune_state.update(
            {
                "status": "training",
                "job_id": job_id,
                "progress": "queued",
                "error": None,
            }
        )

        loop = asyncio.get_event_loop()
        loop.run_in_executor(
            _finetune_executor,
            _run_lora_training,
            str(job_dir),
            adapter_output_dir,
            lora_r,
            lora_alpha,
            num_epochs,
            job_id,
        )

        return JSONResponse(
            content={
                "job_id": job_id,
                "status": "training_started",
                "adapter_output_dir": adapter_output_dir,
            }
        )

    @app.get("/fine-tune/status")
    async def finetune_status():
        """Get current fine-tuning status and progress."""
        return JSONResponse(content=_finetune_state.copy())

    @app.post("/reload-adapter")
    async def reload_adapter(adapter_path: Optional[str] = Form(None)):
        """Hot-reload a LoRA adapter into the running model.

        If adapter_path is not provided, loads from the default latest adapter directory.
        """
        if not service.is_ready or service.transcriber is None:
            raise HTTPException(status_code=503, detail="Service not ready")

        path = adapter_path or str(adapter_base_dir / "latest")

        if not Path(path).exists():
            raise HTTPException(status_code=404, detail=f"Adapter not found at {path}")

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                service.transcriber.load_lora_adapter,
                path,
            )
            return JSONResponse(
                content={
                    "status": "adapter_loaded",
                    "adapter_path": path,
                }
            )
        except Exception as e:
            logger.error(f"Failed to reload adapter: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to load adapter: {e}")


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

    # Add LoRA fine-tuning endpoints
    add_finetune_routes(app, service)

    # Run server
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
