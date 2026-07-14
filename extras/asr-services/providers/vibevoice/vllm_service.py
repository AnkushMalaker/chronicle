"""VibeVoice ASR service (vLLM backend).

A drop-in BaseASRService whose ``provider_name`` is still ``vibevoice`` (so the
backend treats it identically), but which transcribes via a **local vLLM OpenAI
server** running the microsoft ``vllm_plugin`` — single-pass, no windowing.

This process owns the vLLM server's lifecycle: on warmup it ensures the model's
tokenizer files exist (generating them once into the HF cache), launches
``vllm serve`` as a subprocess, waits for it to become healthy, then proxies
``/transcribe`` (and NDJSON progress for long audio) to it via
``VibeVoiceVllmTranscriber``. The standard ``/align`` endpoint (forced alignment
for VibeVoice's missing word timestamps) is served by the FastAPI app as usual.

Why vLLM single-pass: see memory ``vibevoice-vllm-vs-transformers-bench`` — ~3x
faster than the transformers windowed path and far more robust (windowing is what
triggers the collapse; full context does not).
"""

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Optional

import requests
import uvicorn
from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult
from providers.vibevoice.vllm_transcriber import VibeVoiceVllmTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _ensure_tokenizer_files(model_id: str) -> str:
    """Resolve the model snapshot (cached → no download) and generate the
    VibeVoice tokenizer files into it once. Returns the local model path.

    Runs the plugin's generate_tokenizer_files SCRIPT (kept in the image) rather
    than importing it as a module — matches the upstream Dockerfile.vllm and avoids
    relying on ``vllm_plugin`` being importable after a non-editable install."""
    # Lazy import: huggingface_hub isn't a declared top-level dependency of this
    # image (this file's imports are kept minimal); it arrives transitively via
    # vLLM/VibeVoice, so only import it where it's actually used.
    from huggingface_hub import snapshot_download

    model_path = snapshot_download(model_id)
    marker = os.path.join(model_path, ".tokenizer_generated")
    if os.path.exists(marker):
        logger.info("VibeVoice tokenizer files already present, skipping generation")
        return model_path

    script = os.getenv(
        "VIBEVOICE_TOKENIZER_SCRIPT",
        "/opt/VibeVoice/vllm_plugin/tools/generate_tokenizer_files.py",
    )
    logger.info(f"Generating VibeVoice tokenizer files into {model_path}")
    subprocess.run([sys.executable, script, "--output", model_path], check=True)
    try:
        open(marker, "w").close()
    except OSError:
        pass  # read-only cache: regenerating each boot is harmless
    return model_path


class VibeVoiceVllmService(BaseASRService):
    """ASR service using Microsoft VibeVoice-ASR served via vLLM (single-pass)."""

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id or os.getenv("ASR_MODEL", "microsoft/VibeVoice-ASR"))
        self.transcriber: Optional[VibeVoiceVllmTranscriber] = None
        self._vllm_proc: Optional[subprocess.Popen] = None
        self._vllm_port = int(os.getenv("VIBEVOICE_VLLM_PORT", "18000"))
        self._vllm_url = os.getenv(
            "VIBEVOICE_VLLM_URL", f"http://127.0.0.1:{self._vllm_port}"
        )

    @property
    def provider_name(self) -> str:
        return "vibevoice"

    # ------------------------------------------------------------------ lifecycle
    @staticmethod
    def _truthy(val: str) -> bool:
        return str(val).strip().lower() not in ("0", "false", "no", "off", "")

    def _build_vllm_cmd(self, model_path: str) -> list[str]:
        cmd = [
            "vllm",
            "serve",
            model_path,
            "--served-model-name",
            os.getenv("VIBEVOICE_VLLM_SERVED_NAME", "vibevoice"),
            "--trust-remote-code",
            "--dtype",
            os.getenv("VIBEVOICE_TORCH_DTYPE", "bfloat16"),
            "--max-num-seqs",
            os.getenv("VIBEVOICE_VLLM_MAX_NUM_SEQS", "2"),
            "--max-model-len",
            os.getenv("VIBEVOICE_VLLM_MAX_MODEL_LEN", "32768"),
            "--gpu-memory-utilization",
            os.getenv("VIBEVOICE_VLLM_GPU_MEM_UTIL", "0.85"),
            "--no-enable-prefix-caching",
            "--enable-chunked-prefill",
            "--chat-template-content-format",
            "openai",
            "--allowed-local-media-path",
            "/tmp",
            "--port",
            str(self._vllm_port),
        ]
        # enforce-eager (default ON): skip torch.compile + cudagraph capture. On a
        # 24GB card the 18GB VibeVoice model + the encoder profiling forward already
        # crowd VRAM; the cudagraph capture spike then OOMs/hangs startup. Eager costs
        # a little decode speed but we're already ~3x faster than transformers. Set
        # VIBEVOICE_VLLM_ENFORCE_EAGER=0 on a big/dedicated GPU to re-enable graphs.
        if self._truthy(os.getenv("VIBEVOICE_VLLM_ENFORCE_EAGER", "1")):
            cmd.append("--enforce-eager")
        return cmd

    def _launch_vllm(self) -> None:
        model_path = _ensure_tokenizer_files(self.model_id)
        cmd = self._build_vllm_cmd(model_path)
        logger.info(f"Launching vLLM server: {' '.join(cmd)}")
        # New process group so we can signal the whole tree on shutdown. vLLM logs
        # go to our stdout/stderr (captured by the container + system-event reporter).
        self._vllm_proc = subprocess.Popen(cmd, start_new_session=True)

    def _wait_for_vllm(self, timeout: float = 1200.0) -> None:
        """Poll the vLLM server until it serves /v1/models or the timeout elapses."""
        deadline = time.time() + timeout
        url = f"{self._vllm_url}/v1/models"
        last_err = None
        while time.time() < deadline:
            if self._vllm_proc is not None and self._vllm_proc.poll() is not None:
                raise RuntimeError(
                    f"vLLM server exited during startup (code {self._vllm_proc.returncode})"
                )
            try:
                r = requests.get(url, timeout=5)
                if r.status_code == 200:
                    logger.info("vLLM server is healthy")
                    return
            except requests.RequestException as e:
                last_err = e
            time.sleep(5)
        raise RuntimeError(
            f"vLLM server did not become healthy in {timeout}s ({last_err})"
        )

    def _prepare(self) -> None:
        self._launch_vllm()
        self._wait_for_vllm()
        self.transcriber = VibeVoiceVllmTranscriber(base_url=self._vllm_url)
        self.transcriber._is_loaded = True

    async def warmup(self) -> None:
        logger.info(f"Initializing VibeVoice (vLLM) with model: {self.model_id}")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._prepare)
        logger.info("VibeVoice vLLM service ready")

    def shutdown(self) -> None:
        if self._vllm_proc is not None and self._vllm_proc.poll() is None:
            logger.info("Terminating vLLM server subprocess")
            try:
                os.killpg(os.getpgid(self._vllm_proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self._vllm_proc.terminate()

    # ------------------------------------------------------------------ transcribe
    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> TranscriptionResult:
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.transcriber.transcribe(
                audio_file_path, context_info=context_info
            ),
        )

    def get_capabilities(self) -> list[str]:
        return ["timestamps", "diarization", "speaker_identification", "long_form"]

    def supports_batch_progress(self, audio_duration: float) -> bool:
        if self.transcriber is None:
            return False
        return self.transcriber.supports_batch_progress(audio_duration)

    def transcribe_with_progress(
        self, audio_file_path: str, context_info=None, **kwargs
    ):
        if kwargs:
            logger.warning(
                f"transcribe_with_progress: ignoring unsupported kwargs: {list(kwargs.keys())}"
            )
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")
        for event in self.transcriber.transcribe_with_progress(
            audio_file_path, hotwords=context_info
        ):
            if event["type"] == "result":
                yield {"type": "result", **event["result"].to_dict()}
            else:
                yield event


def main():
    parser = argparse.ArgumentParser(description="VibeVoice ASR Service (vLLM)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", required=False)
    args = parser.parse_args()

    if args.model:
        os.environ["ASR_MODEL"] = args.model
    model_id = os.getenv("ASR_MODEL", "microsoft/VibeVoice-ASR")

    service = VibeVoiceVllmService(model_id)
    app = create_asr_app(service)

    @app.on_event("shutdown")
    async def _shutdown():
        service.shutdown()

    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        service.shutdown()


if __name__ == "__main__":
    main()
