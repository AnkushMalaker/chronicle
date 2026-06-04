"""
Gemma 4 ASR Service.

FastAPI service for Google Gemma 4 E2B-it multimodal transcription,
with an OpenAI-compatible chat completions endpoint for unified STT+LLM mode.
"""

import argparse
import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
import wave
from typing import Optional

import uvicorn
from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult
from fastapi import (
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from providers.gemma4.transcriber import Gemma4Transcriber
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Streaming audio is 16-bit LE mono PCM at 16 kHz (Chronicle streaming contract).
STREAM_SAMPLE_RATE = 16000
STREAM_BYTES_PER_SEC = STREAM_SAMPLE_RATE * 2


def _pcm_to_temp_wav(pcm: bytes) -> str:
    """Write raw PCM bytes to a temporary 16 kHz mono WAV file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(STREAM_SAMPLE_RATE)
        wf.writeframes(pcm)
    return path


# --- OpenAI-compatible request/response models ---


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "gemma-4-E2B-it"
    messages: list[dict]
    # Google's recommended sampling configuration for Gemma 4.
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 64
    max_tokens: Optional[int] = None


# --- ASR Service ---


class Gemma4Service(BaseASRService):
    """ASR service using Google Gemma 4 E2B-it multimodal model."""

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.transcriber: Optional[Gemma4Transcriber] = None

    @property
    def provider_name(self) -> str:
        return "gemma4"

    async def warmup(self) -> None:
        logger.info(f"Initializing Gemma 4 with model: {self.model_id}")
        loop = asyncio.get_event_loop()
        self.transcriber = Gemma4Transcriber(self.model_id)
        await loop.run_in_executor(None, self.transcriber.load_model)
        logger.info("Gemma 4 model loaded and ready")

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

    def get_capabilities(self) -> list[str]:
        return ["timestamps", "diarization", "llm"]


def main():
    parser = argparse.ArgumentParser(description="Gemma 4 ASR Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    if args.model:
        os.environ["ASR_MODEL"] = args.model

    model_id = os.getenv("ASR_MODEL", "google/gemma-4-E2B-it")
    service = Gemma4Service(model_id)
    app = create_asr_app(service)

    # --- OpenAI-compatible LLM endpoints (unified STT+LLM mode) ---

    @app.post("/v1/chat/completions")
    async def chat_completions(request: ChatCompletionRequest):
        """OpenAI-compatible chat completions using the loaded Gemma 4 model."""
        if not service.is_ready or service.transcriber is None:
            raise HTTPException(status_code=503, detail="Service not ready")

        loop = asyncio.get_event_loop()
        text, prompt_tokens, completion_tokens = await loop.run_in_executor(
            None,
            lambda: service.transcriber.generate_chat(
                messages=request.messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                top_k=request.top_k,
            ),
        )

        return {
            "id": f"chatcmpl-gemma4-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": service.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    @app.post("/judge")
    async def judge_transcript(
        file: UploadFile = File(...),
        transcript: str = Form(...),
        context: str = Form(""),
        strictness: str = Form("balanced"),
    ):
        """Judge whether a transcript segment is accurate for an audio clip.

        Accepts an audio clip + transcript text + optional surrounding context.
        Returns a verdict with confidence, errors, and reasoning.
        """
        if not service.is_ready or service.transcriber is None:
            raise HTTPException(status_code=503, detail="Service not ready")

        if strictness not in ("strict", "balanced", "lenient"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid strictness: {strictness}. Must be strict, balanced, or lenient.",
            )

        import tempfile

        tmp_filename = None
        try:
            audio_content = await file.read()

            suffix = ".wav"
            if file.filename:
                ext = file.filename.rsplit(".", 1)[-1].lower()
                if ext in ("wav", "mp3", "flac", "ogg", "m4a"):
                    suffix = f".{ext}"

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
                tmp_file.write(audio_content)
                tmp_filename = tmp_file.name

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: service.transcriber.judge_single(
                    tmp_filename, transcript, context, strictness
                ),
            )

            return result

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Judge failed: {e}")
            raise HTTPException(status_code=500, detail=f"Judge failed: {e}")
        finally:
            if tmp_filename:
                try:
                    os.unlink(tmp_filename)
                except Exception:
                    pass

    @app.get("/v1/models")
    async def list_models():
        """List available models (for OpenAI client health checks)."""
        return {
            "object": "list",
            "data": [
                {
                    "id": service.model_id,
                    "object": "model",
                    "owned_by": "google",
                }
            ],
        }

    # --- Streaming-ish WebSocket endpoint (Chronicle stt_stream contract) ---
    #
    # Gemma 4 is a batch ASR model with no incremental decoding and (critically)
    # no word timestamps, so true local-agreement commit (which needs word-level
    # audio offsets to trim the buffer) is not possible. Instead we mirror the
    # qwen3 bridge: re-decode a bounded rolling window every interval to emit
    # interim previews, and run one full (batched) transcription on stream end
    # for the accurate final. The bounded window caps the per-decode cost on
    # long sessions. Reuses the already-loaded model (single process, no bridge).
    stream_interval = float(os.getenv("GEMMA4_STREAM_INTERVAL_SECONDS", "4"))
    stream_window = float(os.getenv("GEMMA4_STREAM_WINDOW_SECONDS", "30"))

    def _transcribe_pcm(pcm: bytes) -> TranscriptionResult:
        path = _pcm_to_temp_wav(pcm)
        try:
            return service.transcriber.transcribe(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        """Streaming transcription: binary PCM in, interim/final JSON out."""
        await ws.accept()
        if not service.is_ready or service.transcriber is None:
            await ws.close(code=1011, reason="Service not ready")
            return

        loop = asyncio.get_event_loop()
        audio = bytearray()
        prev_interim = ""
        last_decoded_bytes = 0
        running = True
        try:
            while running:
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=60.0)
                except asyncio.TimeoutError:
                    continue

                if message.get("type") == "websocket.disconnect":
                    break

                if message.get("bytes"):
                    audio.extend(message["bytes"])
                    pending = (len(audio) - last_decoded_bytes) / STREAM_BYTES_PER_SEC
                    if pending >= stream_interval:
                        last_decoded_bytes = len(audio)
                        window = bytes(
                            audio[-int(stream_window * STREAM_BYTES_PER_SEC) :]
                        )
                        try:
                            result = await loop.run_in_executor(
                                None, _transcribe_pcm, window
                            )
                        except Exception as e:
                            logger.warning(f"gemma4 stream interim failed: {e}")
                            continue
                        text = (result.text or "").strip()
                        if text and text != prev_interim:
                            prev_interim = text
                            await ws.send_json(
                                {
                                    "type": "interim",
                                    "text": text,
                                    "words": [],
                                    "segments": [],
                                }
                            )
                elif message.get("text"):
                    try:
                        msg = json.loads(message["text"])
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") in ("CloseStream", "stop", "finalize"):
                        running = False

            # Accurate final: full-buffer transcription (batches internally >30s).
            if audio:
                result = await loop.run_in_executor(None, _transcribe_pcm, bytes(audio))
                segments = [
                    {
                        "start": s.start,
                        "end": s.end,
                        "text": s.text,
                        "speaker": s.speaker,
                    }
                    for s in (result.segments or [])
                ]
                await ws.send_json(
                    {
                        "type": "final",
                        "text": (result.text or "").strip(),
                        "words": [],
                        "segments": segments,
                    }
                )
            else:
                await ws.send_json(
                    {"type": "final", "text": "", "words": [], "segments": []}
                )
        except WebSocketDisconnect:
            logger.info("gemma4 stream: client disconnected")
        except Exception as e:
            logger.error(f"gemma4 stream error: {e}", exc_info=True)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
