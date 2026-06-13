"""
NeMo ASR Service.

FastAPI service implementation for NVIDIA NeMo ASR provider.
"""

import argparse
import asyncio
import json
import logging
import os
import tempfile
import wave
from typing import Optional

import numpy as np
import uvicorn
from common.base_service import BaseASRService, create_asr_app
from common.response_models import TranscriptionResult, Word
from fastapi import WebSocket, WebSocketDisconnect
from providers.nemo.transcriber import NemoTranscriber

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Chronicle streaming contract: 16-bit LE mono PCM @ 16 kHz.
STREAM_SAMPLE_RATE = 16000
STREAM_BYTES_PER_SEC = STREAM_SAMPLE_RATE * 2


def _pcm_to_float32(pcm: bytes) -> np.ndarray:
    """Convert 16-bit LE PCM bytes to a float32 array in [-1, 1)."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


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


class NemoService(BaseASRService):
    """
    ASR service using NVIDIA NeMo.

    Supports:
    - nvidia/parakeet-tdt-0.6b-v3
    - nvidia/canary-1b
    - Other NeMo ASR models

    Environment variables:
        ASR_MODEL: Model identifier (default: nvidia/parakeet-tdt-0.6b-v3)
        CHUNKING_ENABLED: Enable chunking for long audio (default: true)
        MIN_AUDIO_FOR_CHUNKING: Minimum duration to use chunking (default: 60.0)
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.transcriber: Optional[NemoTranscriber] = None

    @property
    def provider_name(self) -> str:
        return "nemo"

    async def warmup(self) -> None:
        """Initialize and warm up the model."""
        logger.info(f"Initializing NeMo with model: {self.model_id}")

        # Load model (runs in thread pool to not block)
        loop = asyncio.get_event_loop()
        self.transcriber = NemoTranscriber(self.model_id)
        await loop.run_in_executor(None, self.transcriber.load_model)

        # Warm up with short audio
        logger.info("Warming up model...")
        try:
            import numpy as np
            from common.audio_utils import save_to_temp_wav

            # Create 0.1s silence for warmup
            silence = np.zeros(1600, dtype=np.float32)  # 0.1s at 16kHz
            tmp_path = save_to_temp_wav(silence)

            try:
                await self.transcriber.transcribe(tmp_path)
            finally:
                os.unlink(tmp_path)

            logger.info("Model warmed up successfully")
        except Exception as e:
            logger.warning(f"Warmup failed (non-critical): {e}")

    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe audio file. context_info is not used by this provider."""
        if self.transcriber is None:
            raise RuntimeError("Service not initialized")

        return await self.transcriber.transcribe(audio_file_path)

    def get_capabilities(self) -> list[str]:
        return [
            "timestamps",
            "word_timestamps",
            "chunked_processing",
        ]


def main():
    """Main entry point for NeMo service."""
    parser = argparse.ArgumentParser(description="NeMo ASR Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    # Set model via environment if provided
    if args.model:
        os.environ["ASR_MODEL"] = args.model

    # Get model ID (support legacy PARAKEET_MODEL env var)
    model_id = os.getenv("ASR_MODEL") or os.getenv(
        "PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v3"
    )

    # Create service and app
    service = NemoService(model_id)
    app = create_asr_app(service)

    # How often (seconds of newly-arrived audio) to emit an interim preview.
    # ~1s keeps per-step overhead low so decoding stays well ahead of real time
    # and the event loop has gaps to service WebSocket control frames.
    stream_interval = float(os.getenv("NEMO_STREAM_INTERVAL_SECONDS", "1.0"))
    # Rolling window (seconds) used only by the windowed interim *fallback*.
    fallback_window = float(os.getenv("NEMO_FALLBACK_WINDOW_SECONDS", "30"))

    # Single GPU → serialise all model work (interim stream steps + final offline
    # transcribe) so concurrent WebSocket sessions can't race on the shared model.
    model_lock = asyncio.Lock()

    def _offline_transcribe_pcm(pcm: bytes) -> TranscriptionResult:
        """Synchronous full-file transcription of raw PCM (runs in executor)."""
        tr = service.transcriber
        # Prompt models can't use NeMo's offline file path — reuse the
        # streaming-based offline decode (see transcriber.transcribe()).
        if hasattr(tr.model, "set_inference_prompt"):
            path = _pcm_to_temp_wav(pcm)
            try:
                return tr._transcribe_via_streaming(path)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        path = _pcm_to_temp_wav(pcm)
        try:
            with __import__("torch").no_grad():
                results = tr.model.transcribe([path], batch_size=1, timestamps=True)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if not results:
            return TranscriptionResult(text="", words=[], segments=[])
        result = results[0]
        text = getattr(result, "text", result if isinstance(result, str) else "") or ""
        words = []
        ts = getattr(result, "timestamp", None)
        if isinstance(ts, dict) and ts.get("word"):
            for w in ts["word"]:
                words.append(
                    Word(
                        word=w.get("word", ""),
                        start=float(w.get("start", 0.0)),
                        end=float(w.get("end", 0.0)),
                        confidence=1.0,
                    )
                )
        return TranscriptionResult(text=text, words=words, segments=[])

    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        """Streaming transcription: binary PCM in, interim/final JSON out.

        Interim previews use true cache-aware streaming when the model supports
        it (Nemotron 3.5); otherwise a bounded windowed re-decode. The final
        result (what Chronicle stores) is always the accurate full-file decode.
        """
        await ws.accept()
        if not service.is_ready or service.transcriber is None:
            await ws.close(code=1011, reason="Service not ready")
            return

        loop = asyncio.get_event_loop()
        audio = bytearray()  # full session PCM (for the accurate final)
        last_decoded_bytes = 0
        prev_interim = ""
        use_streaming = True

        try:
            session = service.transcriber.new_streaming_session()
        except Exception as e:
            logger.warning(
                f"nemo: cache-aware streaming unavailable ({e}); using windowed "
                "interim decode"
            )
            session = None
            use_streaming = False

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
                    if pending < stream_interval:
                        continue
                    new_pcm = bytes(audio[last_decoded_bytes:])
                    last_decoded_bytes = len(audio)

                    text = ""
                    if use_streaming and session is not None:
                        samples = _pcm_to_float32(new_pcm)
                        try:
                            async with model_lock:
                                text = await loop.run_in_executor(
                                    None, session.add_audio, samples
                                )
                        except Exception as e:
                            logger.warning(
                                f"nemo: stream step failed ({e}); switching to "
                                "windowed interim decode for this session"
                            )
                            use_streaming = False
                            session = None

                    if not use_streaming:
                        window = bytes(
                            audio[-int(fallback_window * STREAM_BYTES_PER_SEC) :]
                        )
                        try:
                            async with model_lock:
                                result = await loop.run_in_executor(
                                    None, _offline_transcribe_pcm, window
                                )
                            text = (result.text or "").strip()
                        except Exception as e:
                            logger.warning(f"nemo interim fallback failed: {e}")
                            continue

                    text = (text or "").strip()
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

            # Final result.
            #   streaming path: the cumulative cache-aware streaming hypothesis
            #     (genuine end-to-end streaming — no full-file re-transcription).
            #   fallback path: one full-file offline decode (only used when the
            #     model isn't cache-aware and interims were already windowed).
            final_text = ""
            final_words: list = []
            if use_streaming and session is not None:
                final_text = (session.text or "").strip()
                final_words = session.get_words()
            elif audio:
                async with model_lock:
                    result = await loop.run_in_executor(
                        None, _offline_transcribe_pcm, bytes(audio)
                    )
                final_text = (result.text or "").strip()
                final_words = [
                    {
                        "word": w.word,
                        "start": w.start,
                        "end": w.end,
                        "confidence": w.confidence,
                    }
                    for w in (result.words or [])
                ]
            await ws.send_json(
                {
                    "type": "final",
                    "text": final_text,
                    "words": final_words,
                    "segments": [],
                }
            )
        except WebSocketDisconnect:
            logger.info("nemo stream: client disconnected")
        except Exception as e:
            logger.error(f"nemo stream error: {e}", exc_info=True)

    # Run server. Disable uvicorn's server-initiated WebSocket keepalive: the
    # GIL-heavy RNNT decode briefly starves the event loop, and a missed pong
    # would otherwise drop a healthy streaming connection mid-decode. Clients
    # that push audio continuously don't need library keepalive.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )


if __name__ == "__main__":
    main()
