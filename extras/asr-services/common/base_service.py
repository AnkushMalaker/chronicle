"""
Abstract base class for ASR services.

Provides a common interface and FastAPI app setup for all ASR providers.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
import wave
from abc import ABC, abstractmethod
from typing import Iterator, Optional

from common.response_models import (
    HealthResponse,
    InfoResponse,
    TranscriptionResult,
    TranscriptionStreamEvent,
)
from common.system_event_reporter import install_system_event_reporter
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# Sentinel returned by _safe_next() when a sync generator is exhausted, so we can
# step it from the event loop via run_in_executor without StopIteration crossing
# the async boundary.
_GEN_DONE = object()


def _safe_next(generator):
    try:
        return next(generator)
    except StopIteration:
        return _GEN_DONE


class BaseASRService(ABC):
    """
    Abstract base class for ASR service implementations.

    Subclasses must implement:
    - transcribe(): Perform transcription on audio file
    - warmup(): Initialize and warm up the model
    - get_model_id(): Return the model identifier
    - get_capabilities(): Return list of supported capabilities
    """

    def __init__(self, model_id: Optional[str] = None):
        """
        Initialize the ASR service.

        Args:
            model_id: Model identifier. If None, reads from ASR_MODEL env var.
        """
        self.model_id = model_id or os.getenv("ASR_MODEL", "")
        self._is_ready = False

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider name (e.g., 'faster-whisper', 'nemo', 'transformers')."""
        pass

    @abstractmethod
    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio file and return result.

        Args:
            audio_file_path: Path to audio file (WAV format, 16kHz mono preferred)
            context_info: Optional hot words / context string for providers that support it
            prompt: Optional custom prompt to override the provider's default transcription prompt

        Returns:
            TranscriptionResult with text, words, segments, etc.
        """
        pass

    @abstractmethod
    async def warmup(self) -> None:
        """
        Initialize and warm up the model.

        Called once during service startup.
        """
        pass

    def get_model_id(self) -> str:
        """Return the current model identifier."""
        return self.model_id

    @abstractmethod
    def get_capabilities(self) -> list[str]:
        """
        Return list of supported capabilities.

        Examples: ['timestamps', 'word_timestamps', 'diarization', 'language_detection']
        """
        pass

    def get_supported_languages(self) -> Optional[list[str]]:
        """
        Return list of supported language codes, or None if multilingual.

        Override in subclasses for models with limited language support.
        """
        return None

    def supports_batch_progress(self, audio_duration: float) -> bool:
        """Return True if this provider reports progress for long audio.

        Providers that batch long audio into windows can override this to
        return True when the audio exceeds their batching threshold.  The
        ``/transcribe`` endpoint uses this to decide whether to return an
        NDJSON streaming response with progress counters.

        Default implementation returns False (no progress reporting).
        """
        return False

    def transcribe_with_progress(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> Iterator[TranscriptionStreamEvent]:
        """Generator that yields progress counters then a final result.

        Only called when ``supports_batch_progress()`` returns True.
        Subclasses that support batch progress must override this.

        Yields:
            TranscriptionProgressEvent
            TranscriptionResultEvent
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement transcribe_with_progress"
        )

    @property
    def is_ready(self) -> bool:
        """Return True if service is ready to handle requests."""
        return self._is_ready

    def max_concurrency(self) -> int:
        """Max concurrent transcriptions this service runs on the GPU at once.

        Default 1: a single-GPU model serializes inference, so concurrent requests
        must queue rather than dogpile the GPU (which causes VRAM OOM / wedge).
        Configurable via the ASR_MAX_CONCURRENCY env var, or override in a provider
        to derive the limit from available VRAM (memory-aware concurrency).
        """
        try:
            return max(1, int(os.getenv("ASR_MAX_CONCURRENCY", "1")))
        except ValueError:
            return 1

    def priority_concurrency(self) -> int:
        """Concurrent slots reserved for the PRIORITY lane (min 1).

        The priority lane is a separate GPU semaphore from the normal lane, so a
        latency-sensitive request (e.g. a wake-word command clip) always has a
        free slot and runs concurrently with a long batch instead of queueing
        behind it. On a single GPU this means up to (normal + priority) inferences
        interleave — keep this at 1 and priority clips short to bound VRAM use.
        Configurable via ASR_PRIORITY_CONCURRENCY.
        """
        try:
            return max(1, int(os.getenv("ASR_PRIORITY_CONCURRENCY", "1")))
        except ValueError:
            return 1


def _get_audio_duration(file_path: str) -> Optional[float]:
    """Return audio duration in seconds, or None if unreadable."""
    try:
        with wave.open(file_path, "rb") as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return None


def create_asr_app(service: BaseASRService) -> FastAPI:
    """
    Create a FastAPI application with standard ASR endpoints.

    Args:
        service: Initialized ASR service instance

    Returns:
        Configured FastAPI application
    """
    app = FastAPI(
        title=f"{service.provider_name.title()} ASR Service",
        version="1.0.0",
        description=f"ASR service using {service.provider_name} provider",
    )

    # Forward this service's ERROR/CRITICAL logs to the backend's system-event
    # ledger so failures here surface on the admin "System Errors" page. No-op
    # unless CHRONICLE_INGEST_URL/CHRONICLE_INGEST_TOKEN are configured.
    install_system_event_reporter(source=f"asr-{service.provider_name}")

    # GPU concurrency gates: limit how many transcriptions run at once so a burst of
    # requests queues at the GPU instead of dogpiling it (which OOMs/wedges a single-GPU
    # service). Two independent lanes — "normal" and "priority" — each with its own
    # semaphore (≥1), so a latency-sensitive request gets a dedicated slot and runs
    # concurrently with a long batch instead of queueing behind it. Created on startup
    # so they bind to the running event loop. Shared by the streaming and single paths.
    gate = {}

    # Lazily-loaded forced aligner (MMS_FA). Shared across /align requests; loaded on
    # first use so providers that never align don't pay the model-load cost.
    aligner_holder = {}
    aligner_lock = asyncio.Lock()

    @app.on_event("startup")
    async def startup_event():
        """Initialize the transcriber on startup."""
        logger.info(f"Starting {service.provider_name} ASR service...")
        await service.warmup()
        normal_limit = service.max_concurrency()
        priority_limit = service.priority_concurrency()
        gate["normal"] = asyncio.Semaphore(normal_limit)
        gate["priority"] = asyncio.Semaphore(priority_limit)
        # Mark ready only after the gates exist, so the /transcribe handler never
        # races ahead of them being set.
        service._is_ready = True
        logger.info(
            f"{service.provider_name} ASR service ready "
            f"(normal concurrency: {normal_limit}, priority: {priority_limit})"
        )

    @app.get("/health", response_model=HealthResponse)
    async def health_check():
        """Health check endpoint."""
        return HealthResponse(
            status="healthy" if service.is_ready else "initializing",
            model=service.get_model_id(),
            provider=service.provider_name,
        )

    @app.get("/info", response_model=InfoResponse)
    async def service_info():
        """Service information endpoint."""
        return InfoResponse(
            model_id=service.get_model_id(),
            provider=service.provider_name,
            capabilities=service.get_capabilities(),
            supported_languages=service.get_supported_languages(),
        )

    @app.post("/transcribe")
    async def transcribe(
        file: UploadFile = File(...),
        context_info: Optional[str] = Form(None),
        prompt: Optional[str] = Form(None),
        priority: bool = Form(False),
    ):
        """
        Transcribe uploaded audio file.

        Accepts audio files (WAV, MP3, etc.) and returns transcription
        with word-level timestamps. Optionally accepts context_info
        (hot words, speaker names, topics) and a custom prompt for
        providers that support it.
        """
        if not service.is_ready:
            raise HTTPException(status_code=503, detail="Service not ready")

        # Pick the GPU lane: priority requests use a dedicated semaphore so they
        # never queue behind a long batch holding the normal lane.
        sem = gate["priority"] if priority else gate["normal"]

        request_start = time.time()
        logger.info(f"Transcription request started (priority={priority})")

        tmp_filename = None
        streaming_response = False
        try:
            # Read uploaded file
            file_read_start = time.time()
            audio_content = await file.read()
            file_read_time = time.time() - file_read_start
            logger.info(
                f"File read completed in {file_read_time:.3f}s "
                f"(size: {len(audio_content)} bytes)"
            )

            # Save to temporary file
            suffix = ".wav"
            if file.filename:
                ext = file.filename.rsplit(".", 1)[-1].lower()
                if ext in ("wav", "mp3", "flac", "ogg", "m4a"):
                    suffix = f".{ext}"

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
                tmp_file.write(audio_content)
                tmp_filename = tmp_file.name

            # Check if provider supports batch progress for this audio
            audio_duration = _get_audio_duration(tmp_filename)
            if audio_duration and service.supports_batch_progress(audio_duration):
                logger.info(
                    f"Audio is {audio_duration:.1f}s, using batch progress reporting"
                )
                streaming_response = True

                async def _ndjson_generator():
                    """Stream NDJSON progress/result, holding the GPU gate for the
                    whole inference and stepping the provider's sync generator in a
                    thread so the event loop stays responsive."""
                    loop = asyncio.get_event_loop()
                    async with sem:
                        sync_gen = service.transcribe_with_progress(
                            tmp_filename,
                            context_info=context_info,
                            prompt=prompt,
                        )
                        try:
                            while True:
                                event = await loop.run_in_executor(
                                    None, _safe_next, sync_gen
                                )
                                if event is _GEN_DONE:
                                    break
                                yield (
                                    json.dumps(
                                        event.model_dump(mode="json", exclude_none=True)
                                    )
                                    + "\n"
                                )
                        finally:
                            try:
                                os.unlink(tmp_filename)
                            except Exception as e:
                                logger.warning(
                                    f"Failed to delete temp file {tmp_filename}: {e}"
                                )

                return StreamingResponse(
                    _ndjson_generator(),
                    media_type="application/x-ndjson",
                )

            # Normal path: single JSON response. Hold the GPU gate across the call so
            # short-audio requests serialize with streaming ones on the single GPU.
            transcribe_start = time.time()
            async with sem:
                result = await service.transcribe(
                    tmp_filename,
                    context_info=context_info,
                    prompt=prompt,
                )
            transcribe_time = time.time() - transcribe_start
            logger.info(f"Transcription completed in {transcribe_time:.3f}s")

            total_time = time.time() - request_start
            logger.info(f"Total request time: {total_time:.3f}s")

            return JSONResponse(content=result.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            error_time = time.time() - request_start
            logger.exception(f"Error after {error_time:.3f}s: {e}")
            raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")

        finally:
            # Streaming path owns its own cleanup via the generator's finally block.
            # Only clean up here for the normal (non-streaming) path.
            if tmp_filename and not streaming_response:
                try:
                    os.unlink(tmp_filename)
                except Exception as e:
                    logger.warning(f"Failed to delete temp file {tmp_filename}: {e}")

    @app.post("/align")
    async def align(
        file: UploadFile = File(...),
        segments: str = Form(...),
    ):
        """Forced-align known segment text to audio, returning word-level timestamps.

        For providers (e.g. VibeVoice) that emit segment-level timestamps + text but no
        per-word timing. ``segments`` is a JSON array of {start, end, text}; each
        segment's text is aligned within its own audio window (more accurate and avoids
        CTC "targets too long" on long files), and word times are returned in absolute
        conversation time. This lets downstream re-diarization re-attach transcript text
        to fresh speaker boundaries.
        """
        # Lazy import: base_service.py is shared by every provider image, including
        # ones that don't install torch/numpy/torchaudio. Only providers that call
        # /align need them.
        import numpy as np
        import torch
        import torchaudio  # only torchaudio.functional.resample (no codec dependency)
        from common.forced_align import ForcedAligner

        try:
            seg_list = json.loads(segments)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid segments JSON: {e}")
        if not isinstance(seg_list, list):
            raise HTTPException(status_code=400, detail="segments must be a JSON array")

        # Lazy-load the aligner once (double-checked under lock).
        if "fa" not in aligner_holder:
            async with aligner_lock:
                if "fa" not in aligner_holder:
                    fa = ForcedAligner()
                    await asyncio.get_event_loop().run_in_executor(None, fa.load)
                    aligner_holder["fa"] = fa
        fa: ForcedAligner = aligner_holder["fa"]

        tmp_filename = None
        try:
            audio_content = await file.read()
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                tmp_file.write(audio_content)
                tmp_filename = tmp_file.name

            # Read WAV via stdlib `wave` (NOT torchaudio.load — newer torchaudio routes
            # load() through torchcodec, which isn't installed). Audio arrives as 16 kHz
            # mono PCM16 WAV from the backend's reconstruct_audio_segment.
            with wave.open(tmp_filename, "rb") as wf:
                sr = wf.getframerate()
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                raw = wf.readframes(wf.getnframes())
            if sampwidth != 2:
                raise HTTPException(
                    status_code=400,
                    detail=f"Expected 16-bit PCM WAV, got sample width {sampwidth}",
                )
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if n_channels > 1:  # downmix to mono
                audio = audio.reshape(-1, n_channels).mean(axis=1)
            wav = torch.from_numpy(audio).unsqueeze(0)  # (1, N)
            if sr != fa.sample_rate:
                wav = torchaudio.functional.resample(wav, sr, fa.sample_rate)
            sr = fa.sample_rate
            total_dur = wav.size(1) / sr

            def _fill_none(times, seg_start, seg_end):
                """Offset window-relative times to absolute and interpolate gaps
                (words that didn't tokenize, e.g. digits/punctuation)."""
                n = len(times)
                res = [None] * n
                for i, t in enumerate(times):
                    if t is not None:
                        res[i] = (t[0] + seg_start, t[1] + seg_start)
                last_end = seg_start
                i = 0
                while i < n:
                    if res[i] is not None:
                        last_end = res[i][1]
                        i += 1
                        continue
                    j = i
                    while j < n and res[j] is None:
                        j += 1
                    next_start = res[j][0] if j < n else seg_end
                    span = max(0.0, next_start - last_end)
                    step = span / (j - i) if j > i else 0.0
                    for k in range(i, j):
                        res[k] = (
                            last_end + step * (k - i),
                            last_end + step * (k - i + 1),
                        )
                    last_end = next_start
                    i = j
                return res

            def _do_align():
                out_words = []
                aligned_segs = 0
                for seg in seg_list:
                    text = (seg.get("text") or "").strip()
                    if not text:
                        continue
                    s = max(0.0, float(seg.get("start", 0.0)))
                    e = min(total_dur, float(seg.get("end", s)))
                    if e <= s:
                        continue
                    toks = text.split()
                    if not toks:
                        continue
                    a, b = int(s * sr), int(e * sr)
                    window = wav[:, a:b]
                    if window.size(1) < int(0.05 * sr):  # < 50ms, too short to align
                        continue
                    times = fa.align(window, toks)
                    abs_times = _fill_none(times, s, e)
                    for tok, tm in zip(toks, abs_times):
                        out_words.append(
                            {
                                "word": tok,
                                "start": round(float(tm[0]), 3),
                                "end": round(float(tm[1]), 3),
                                "confidence": None,
                            }
                        )
                    aligned_segs += 1
                return out_words, aligned_segs

            # Serialize on the GPU's normal lane so alignment doesn't dogpile inference.
            async with gate["normal"]:
                words, aligned_segs = await asyncio.get_event_loop().run_in_executor(
                    None, _do_align
                )

            logger.info(
                f"Forced alignment: {len(words)} words across {aligned_segs}"
                f"/{len(seg_list)} segments ({total_dur:.1f}s audio)"
            )
            return JSONResponse(
                content={
                    "words": words,
                    "duration": round(total_dur, 3),
                    "aligned_segments": aligned_segs,
                    "total_segments": len(seg_list),
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Forced alignment failed: {e}")
            raise HTTPException(status_code=500, detail=f"Alignment failed: {e}")
        finally:
            if tmp_filename:
                try:
                    os.unlink(tmp_filename)
                except Exception as e:
                    logger.warning(f"Failed to delete temp file {tmp_filename}: {e}")

    return app
