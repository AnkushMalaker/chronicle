#!/usr/bin/env python3
"""
FastAPI HTTP/WebSocket ASR server using NeMo Parakeet ASR + Silero VAD.

Provides both batch transcription (HTTP POST) and streaming transcription (WebSocket)
with configurable processing triggers (VAD + time-based) and interim results.

Dependencies are managed via pyproject.toml with uv.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, cast

import nemo.collections.asr as nemo_asr
import numpy as np
import torch
import uvicorn
from easy_audio_interfaces.audio_interfaces import ResamplingBlock
from easy_audio_interfaces.filesystem import LocalFileSink, LocalFileStreamer

# Import enhanced chunking modules
from enhanced_chunking import (
    TimestampedFrameBatchChunkedRNNT,
    extract_timestamps_from_hypotheses,
    transcribe_with_enhanced_chunking,
)
from fastapi import (
    FastAPI,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from silero_vad import VADIterator, load_silero_vad
from wyoming.audio import AudioChunk

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------- #
PARAKEET_SAMPLING_RATE = VAD_SAMPLING_RATE = 16_000
CHUNK_SAMPLES = 512  # Silero requirement (32 ms @ 16 kHz)
MAX_SPEECH_SECS = 120  # Max duration of a speech segment
MIN_SPEECH_SECS = 0.5  # Min duration for transcription

# Enhanced chunking configuration
CHUNKING_ENABLED = os.getenv("CHUNKING_ENABLED", "true").lower() == "true"
CHUNK_DURATION_SECONDS = float(os.getenv("CHUNK_DURATION_SECONDS", "30.0"))
OVERLAP_DURATION_SECONDS = float(os.getenv("OVERLAP_DURATION_SECONDS", "5.0"))
MIN_AUDIO_FOR_CHUNKING = float(
    os.getenv("MIN_AUDIO_FOR_CHUNKING", "60.0")
)  # Use chunking for audio > 60s
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))


@dataclass
class ProcessEventConfig:
    """Configuration for streaming processing triggers."""

    vad_enabled: bool = False
    vad_silence_ms: int = 1000  # Silence duration to trigger processing
    time_interval_seconds: int = 30  # Max time between processing
    max_buffer_seconds: int = 120  # Force processing after this duration
    return_interim_results: bool = True  # Send partial results during streaming
    min_audio_seconds: float = 0.5  # Minimum audio length to process


def _chunk_to_numpy_float(chunk: AudioChunk) -> np.ndarray:
    """Convert AudioChunk to numpy float32 array."""
    if chunk.channels != 1:
        raise ValueError(f"Unsupported channels: {chunk.channels}")
    if chunk.rate != PARAKEET_SAMPLING_RATE:
        raise ValueError(f"Unsupported sampling rate: {chunk.rate}")
    if chunk.width == 2:
        logger.debug(f"Converting chunk to float32")
        return (
            np.array(np.frombuffer(chunk.audio, dtype=np.int16), dtype=np.float32)
            / np.iinfo(np.int16).max
        )
    elif chunk.width == 4:
        logger.debug(f"Converting chunk to float32")
        return (
            np.array(np.frombuffer(chunk.audio, dtype=np.int32), dtype=np.float32)
            / np.iinfo(np.int32).max
        )
    else:
        raise ValueError(f"Unsupported width: {chunk.width}")


# --------------------------------------------------------------------------- #
class SharedTranscriber:
    """Shared transcriber instance that can be used by multiple clients."""

    def __init__(self, model_name: str):
        logger.info(f"Loading shared Nemo ASR model: {model_name}")
        self.model = cast(
            nemo_asr.models.ASRModel,
            nemo_asr.models.ASRModel.from_pretrained(model_name=model_name),
        )
        self._rate = PARAKEET_SAMPLING_RATE
        self._model_name = model_name
        self._lock = asyncio.Lock()

        # Chunking components are now handled by enhanced_chunking.transcribe_with_enhanced_chunking
        self.chunked_processor = None  # Will be initialized when needed

    async def warmup(self) -> None:
        """Warm up the ASR model."""
        logger.info("Warming up shared NeMo ASR model...")
        try:
            await self._transcribe(
                [
                    AudioChunk(
                        audio=np.zeros(self._rate // 10, np.int16).tobytes(),
                        rate=self._rate,
                        channels=1,
                        width=2,
                    )
                ]
            )  # 0.1s silence
            logger.info("Shared NeMo ASR model warmed up successfully.")
        except Exception as e:
            logger.error(f"Error during ASR model warm-up: {e}")

    async def _transcribe_chunked(self, speech: Sequence[AudioChunk]) -> dict:
        """Chunked transcription method for long audio."""
        logger.info("Using chunked transcription for long audio")

        # Save audio to temporary file for NeMo processing
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmpfile:
            tmpfile_name = tmpfile.name

            # Convert audio chunks to numpy array and save as WAV
            audio_arrays = []
            for chunk in speech:
                if chunk.width == 2:
                    audio_array = (
                        np.frombuffer(chunk.audio, dtype=np.int16).astype(np.float32)
                        / np.iinfo(np.int16).max
                    )
                elif chunk.width == 4:
                    audio_array = (
                        np.frombuffer(chunk.audio, dtype=np.int32).astype(np.float32)
                        / np.iinfo(np.int32).max
                    )
                else:
                    raise ValueError(f"Unsupported width: {chunk.width}")
                audio_arrays.append(audio_array)

            # Concatenate and save as WAV file
            full_audio = np.concatenate(audio_arrays)

            # Convert to int16 for WAV format
            audio_int16 = (full_audio * np.iinfo(np.int16).max).astype(np.int16)

            with wave.open(tmpfile_name, "wb") as wav_file:
                wav_file.setnchannels(1)  # Mono
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(self._rate)
                wav_file.writeframes(audio_int16.tobytes())

        try:
            # Use the enhanced chunking function for proper NeMo integration
            logger.info("🔍 ISOLATE: About to call transcribe_with_enhanced_chunking")
            result = await transcribe_with_enhanced_chunking(
                model=self.model,
                audio_file_path=tmpfile_name,
                frame_len=4.0,  # 4-second frames
                total_buffer=8.0,  # 8-second total buffer
            )

            logger.info(
                f"🔍 ISOLATE: transcribe_with_enhanced_chunking returned: {type(result)}"
            )

            if result is None:
                logger.error("🔍 ISOLATE: result is None!")
                return {"text": "", "words": [], "segments": []}

            logger.info(
                f"🔍 ISOLATE: result.get('words') type: {type(result.get('words'))}"
            )
            words = result.get("words", [])
            logger.info(f"🔍 ISOLATE: words variable type: {type(words)}")

            if words is None:
                logger.warning("🔍 ISOLATE: words is None, using empty list")
                words = []

            logger.info(f"🔍 ISOLATE: About to call len() on words: {type(words)}")
            word_count = len(words) if words else 0
            logger.info(f"Enhanced chunking completed: {word_count} words")
            return result

        finally:
            # Clean up temporary file
            try:
                os.unlink(tmpfile_name)
            except Exception as e:
                logger.warning(f"Failed to delete temporary file {tmpfile_name}: {e}")

    async def _transcribe(self, speech: Sequence[AudioChunk]) -> dict:
        """Internal transcription method that returns structured result."""
        assert len(speech) > 0

        # Calculate total audio duration
        total_duration = sum(
            len(chunk.audio) / (chunk.rate * chunk.width * chunk.channels)
            for chunk in speech
        )

        # Use chunked processing for long audio if enabled
        if CHUNKING_ENABLED and total_duration > MIN_AUDIO_FOR_CHUNKING:
            logger.info(
                f"Audio duration {total_duration:.1f}s > {MIN_AUDIO_FOR_CHUNKING}s threshold - using chunked processing"
            )
            return await self._transcribe_chunked(speech)

        logger.info(
            f"Audio duration {total_duration:.1f}s - using single-pass processing"
        )

        sample_rate = speech[0].rate
        channels = speech[0].channels
        sample_width = speech[0].width
        tmpfile_name = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmpfile:
                tmpfile_name = tmpfile.name

            logger.info(f"Writing to file: {tmpfile_name}")
            sink = LocalFileSink(
                file_path=tmpfile_name,
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
            )
            assert self._rate == sample_rate
            await sink.open()
            for chunk in speech:
                await sink.write(chunk)
            await sink.close()

            with torch.no_grad():
                results = self.model.transcribe(  # type: ignore
                    [tmpfile_name], batch_size=1, timestamps=True
                )
            logger.info(f"Transcription results: {results}")

            if results and len(results) > 0:
                result = results[0]

                # Extract text
                if hasattr(result, "text") and result.text:
                    text = result.text
                elif isinstance(result, str):
                    text = result
                else:
                    text = ""

                # Extract word-level timestamps - NeMo Parakeet format
                words = []
                for word_data in result.timestamp["word"]:
                    word_dict = {
                        "word": word_data["word"],
                        "start": word_data["start"],
                        "end": word_data["end"],
                        "confidence": 1.0,
                    }
                    words.append(word_dict)

                logger.info(
                    f"NeMo transcription successful: {len(text)} chars, {len(words)} words"
                )

                response_data = {
                    "text": text,
                    "words": words,
                    "segments": [],  # Empty for non-RTTM providers
                }

                # LOG THE FULL RESPONSE FOR DEBUGGING
                logger.info(f"🔍 PARAKEET RESPONSE DEBUG:")
                logger.info(f"  - Text length: {len(response_data['text'])}")
                logger.info(f"  - First 100 chars: {repr(response_data['text'][:100])}")
                logger.info(f"  - Words count: {len(response_data['words'])}")
                logger.info(
                    f"  - First 3 words: {response_data['words'][:3] if response_data['words'] else 'None'}"
                )
                logger.info(f"  - Full response keys: {list(response_data.keys())}")

                return response_data
            else:
                logger.warning("NeMo returned empty results")
                return {"text": "", "words": [], "segments": []}

        except Exception as e:
            logger.error(f"Error during transcription: {e}")
            # Re-raise the exception so HTTP endpoint can return proper error code
            raise e
        finally:
            if tmpfile_name and os.path.exists(tmpfile_name):
                os.remove(tmpfile_name)

    async def transcribe_async(self, speech: Sequence[AudioChunk]) -> dict:
        """Thread-safe async transcription method."""
        async with self._lock:
            return await self._transcribe(speech)


# --------------------------------------------------------------------------- #
class StreamingSession:
    """Manages a single streaming transcription session."""

    def __init__(
        self,
        session_id: str,
        config: ProcessEventConfig,
        transcriber: SharedTranscriber,
    ):
        self.session_id = session_id
        self.config = config
        self.transcriber = transcriber
        self.audio_chunks: List[AudioChunk] = []
        self.last_process_time = time.time()
        self.created_at = time.time()

        # Resampling setup - will be configured on first chunk
        self.resampler = ResamplingBlock(
            resample_rate=PARAKEET_SAMPLING_RATE, resample_channels=1
        )
        self.input_rate = None  # Will be set from first chunk
        self.needs_resampling = False

        # VAD setup
        if config.vad_enabled:
            self.vad_model = load_silero_vad(onnx=True)
            self.vad_iterator = VADIterator(
                model=self.vad_model,
                sampling_rate=VAD_SAMPLING_RATE,
                threshold=0.4,
                min_silence_duration_ms=config.vad_silence_ms,
            )
            self.vad_sample_buffer = np.array([], dtype=np.float32)
        else:
            self.vad_model = None
            self.vad_iterator = None
            self.vad_sample_buffer = None

    async def add_audio_chunk(
        self, audio_data: bytes, rate: int, width: int, channels: int
    ) -> bool:
        """Add audio chunk with resampling and check if processing should be triggered."""

        # Detect format on first chunk
        if self.input_rate is None:
            self.input_rate = rate
            self.needs_resampling = rate != PARAKEET_SAMPLING_RATE or channels != 1
            if self.needs_resampling:
                logger.info(
                    f"Session {self.session_id}: Resampling {rate}Hz/{channels}ch → {PARAKEET_SAMPLING_RATE}Hz/1ch"
                )
            else:
                logger.info(
                    f"Session {self.session_id}: No resampling needed - audio already at 16kHz mono"
                )

        # Create input chunk
        input_chunk = AudioChunk(
            audio=audio_data, rate=rate, width=width, channels=channels
        )

        # Resample if needed and keep track of processed chunk for VAD
        processed_chunk = None
        if self.needs_resampling:
            async for resampled_chunk in self.resampler.process_chunk(input_chunk):
                self.audio_chunks.append(resampled_chunk)
                processed_chunk = resampled_chunk  # Use last resampled chunk for VAD
        else:
            self.audio_chunks.append(input_chunk)
            processed_chunk = input_chunk  # Use input chunk for VAD

        # Check processing triggers
        current_time = time.time()

        # Time-based trigger
        time_trigger = (
            current_time - self.last_process_time
        ) > self.config.time_interval_seconds

        # Buffer size trigger
        buffer_duration = sum(
            len(chunk.audio) / (chunk.rate * chunk.width * chunk.channels)
            for chunk in self.audio_chunks
        )
        buffer_trigger = buffer_duration > self.config.max_buffer_seconds

        # VAD trigger
        vad_trigger = False
        if self.config.vad_enabled and self.vad_iterator and processed_chunk:
            try:
                # Convert chunk to float for VAD
                audio_float = _chunk_to_numpy_float(processed_chunk)
                if self.vad_sample_buffer is not None:
                    self.vad_sample_buffer = np.append(
                        self.vad_sample_buffer, audio_float
                    )

                # Process in CHUNK_SAMPLES increments
                while len(self.vad_sample_buffer) >= CHUNK_SAMPLES:
                    samples = self.vad_sample_buffer[:CHUNK_SAMPLES]
                    self.vad_sample_buffer = self.vad_sample_buffer[CHUNK_SAMPLES:]

                    speech_dict = self.vad_iterator(samples)
                    if "end" in speech_dict:
                        vad_trigger = True
                        break

            except Exception as e:
                logger.warning(f"VAD processing error: {e}")

        return time_trigger or buffer_trigger or vad_trigger

    async def process_buffered_audio(self) -> dict:
        """Process all buffered audio and return transcription."""
        if not self.audio_chunks:
            return {"text": "", "words": [], "segments": []}

        # Check minimum duration
        total_duration = sum(
            len(chunk.audio) / (chunk.rate * chunk.width * chunk.channels)
            for chunk in self.audio_chunks
        )

        if total_duration < self.config.min_audio_seconds:
            logger.debug(
                f"Audio too short ({total_duration:.2f}s), skipping processing"
            )
            return {"text": "", "words": [], "segments": []}

        logger.info(
            f"Processing {len(self.audio_chunks)} chunks ({total_duration:.2f}s) for session {self.session_id}"
        )

        # Transcribe buffered audio
        result = await self.transcriber.transcribe_async(self.audio_chunks)

        # Clear processed chunks (keep small buffer for continuity)
        processed_chunks = len(self.audio_chunks)
        self.audio_chunks = self.audio_chunks[-2:] if len(self.audio_chunks) > 2 else []
        self.last_process_time = time.time()

        logger.info(
            f"Processed {processed_chunks} chunks, result: {len(result.get('text', ''))} chars"
        )
        return result


# --------------------------------------------------------------------------- #
# FastAPI App
app = FastAPI(title="Parakeet ASR Service", version="1.0.0")

# Global transcriber instance
transcriber: Optional[SharedTranscriber] = None
active_sessions: Dict[str, StreamingSession] = {}


@app.on_event("startup")
async def startup_event():
    """Initialize the transcriber on startup."""
    global transcriber
    model_name = str(os.getenv("PARAKEET_MODEL"))
    transcriber = SharedTranscriber(model_name)
    await transcriber.warmup()
    logger.info("Parakeet ASR service started")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "model": transcriber._model_name if transcriber else "not_loaded",
    }


@app.post("/transcribe")
async def batch_transcribe(file: UploadFile = File(...)):
    """Batch transcription endpoint."""
    if not transcriber:
        raise HTTPException(status_code=503, detail="Transcriber not initialized")

    request_start = time.time()
    logger.info(
        f"🕐 TIMING: Transcription request started at {time.strftime('%H:%M:%S', time.localtime(request_start))}"
    )

    try:
        # Read uploaded file
        file_read_start = time.time()
        audio_content = await file.read()
        file_read_time = time.time() - file_read_start
        logger.info(
            f"🕐 TIMING: File read completed in {file_read_time:.3f}s (size: {len(audio_content)} bytes)"
        )

        # Save to temporary file for processing
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_content)
            tmp_filename = tmp_file.name

        try:
            # TIMING: LocalFileStreamer creation
            streamer_create_start = time.time()
            streamer = LocalFileStreamer(tmp_filename, chunk_size_samples=CHUNK_SAMPLES)
            streamer_create_time = time.time() - streamer_create_start
            logger.info(
                f"🕐 TIMING: LocalFileStreamer creation: {streamer_create_time:.3f}s"
            )

            # TIMING: streamer.open()
            streamer_open_start = time.time()
            await streamer.open()
            streamer_open_time = time.time() - streamer_open_start
            logger.info(f"🕐 TIMING: LocalFileStreamer open: {streamer_open_time:.3f}s")

            # TIMING: First chunk read
            first_chunk_start = time.time()
            first_chunk = await streamer.read()
            first_chunk_time = time.time() - first_chunk_start
            logger.info(f"🕐 TIMING: First chunk read: {first_chunk_time:.3f}s")

            original_rate = first_chunk.rate
            original_channels = first_chunk.channels
            original_width = first_chunk.width

            logger.info(
                f"Input audio: {original_rate}Hz, {original_channels}ch, {original_width*8}bit"
            )

            # TIMING: Collect all chunks using iter_frames()
            iter_frames_start = time.time()
            all_chunks = [first_chunk]  # Include the first chunk we already read
            chunk_count = 1
            async for chunk in streamer.iter_frames():
                all_chunks.append(chunk)
                chunk_count += 1
                # Progress every 10,000 chunks
                if chunk_count % 10000 == 0:
                    elapsed = time.time() - iter_frames_start
                    logger.info(
                        f"🕐 TIMING: Loaded {chunk_count} chunks in {elapsed:.3f}s so far..."
                    )

            iter_frames_time = time.time() - iter_frames_start
            logger.info(
                f"🕐 TIMING: iter_frames() completed: {iter_frames_time:.3f}s for {len(all_chunks)} chunks"
            )
            logger.info(
                f"Loaded {len(all_chunks)} audio chunks, total duration: {sum(len(c.audio)/(c.rate*c.width*c.channels) for c in all_chunks):.2f}s"
            )

            # TIMING: Setup resampling
            resampling_setup_start = time.time()
            needs_resampling = (
                original_rate != PARAKEET_SAMPLING_RATE or original_channels != 1
            )
            resampling_setup_time = time.time() - resampling_setup_start
            logger.info(
                f"🕐 TIMING: Resampling setup check: {resampling_setup_time:.3f}s"
            )

            if needs_resampling:
                logger.info(
                    f"Resampling from {original_rate}Hz/{original_channels}ch to {PARAKEET_SAMPLING_RATE}Hz/1ch"
                )

                # TIMING: ResamplingBlock creation
                resampler_create_start = time.time()
                resampler = ResamplingBlock(
                    resample_rate=PARAKEET_SAMPLING_RATE, resample_channels=1
                )
                resampler_create_time = time.time() - resampler_create_start
                logger.info(
                    f"🕐 TIMING: ResamplingBlock creation: {resampler_create_time:.3f}s"
                )

                # TIMING: Resample all chunks
                resampling_start = time.time()
                resampled_chunks = []
                for i, chunk in enumerate(all_chunks):
                    if i % 10000 == 0 and i > 0:
                        elapsed = time.time() - resampling_start
                        logger.info(
                            f"🕐 TIMING: Resampled {i}/{len(all_chunks)} chunks in {elapsed:.3f}s so far..."
                        )

                    async for resampled_chunk in resampler.process_chunk(chunk):
                        resampled_chunks.append(resampled_chunk)

                resampling_time = time.time() - resampling_start
                logger.info(f"🕐 TIMING: Resampling completed: {resampling_time:.3f}s")
                final_chunks = resampled_chunks
                logger.info(
                    f"Resampled to {len(final_chunks)} chunks at {PARAKEET_SAMPLING_RATE}Hz"
                )
            else:
                final_chunks = all_chunks
                logger.info("No resampling needed - audio already at 16kHz mono")

            # Transcribe with all chunks (assertion will verify they're all 16kHz)
            transcribe_start = time.time()
            logger.info(
                f"🕐 TIMING: Starting NeMo transcription with {len(final_chunks)} chunks"
            )
            result = await transcriber.transcribe_async(final_chunks)
            transcribe_time = time.time() - transcribe_start
            logger.info(f"🕐 TIMING: Transcription completed in {transcribe_time:.3f}s")

            total_time = time.time() - request_start
            logger.info(f"🕐 TIMING: Total request time: {total_time:.3f}s")
            return JSONResponse(content=result)

        finally:
            os.unlink(tmp_filename)

    except Exception as e:
        error_time = time.time() - request_start
        if isinstance(e, HTTPException):
            raise
        logger.exception(f"🕐 TIMING: Error occurred after {error_time:.3f}s - {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.websocket("/stream")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket streaming transcription endpoint."""
    if not transcriber:
        await websocket.close(code=1011, reason="Transcriber not initialized")
        return

    await websocket.accept()
    logger.info("WebSocket connection established")

    session_id: Optional[str] = None
    session: Optional[StreamingSession] = None

    try:
        while True:
            # Receive message
            message = await websocket.receive_text()
            data = json.loads(message)

            if data["type"] == "transcribe":
                # Start new session
                session_id = data.get("session_id", str(uuid.uuid4()))
                config_data = data.get("config", {})
                config = ProcessEventConfig(**config_data)

                session = StreamingSession(session_id, config, transcriber)
                active_sessions[session_id] = session

                logger.info(
                    f"Started streaming session {session_id} with config: {config}"
                )
                await websocket.send_text(
                    json.dumps({"type": "session_started", "session_id": session_id})
                )

            elif data["type"] == "audio_chunk":
                if not session:
                    logger.warning("Received audio_chunk without active session")
                    continue

                # Receive binary audio data
                audio_data = await websocket.receive_bytes()

                # Add chunk and check if processing should be triggered
                rate = data.get("rate", 16000)
                width = data.get("width", 2)
                channels = data.get("channels", 1)

                should_process = await session.add_audio_chunk(
                    audio_data, rate, width, channels
                )

                if should_process and session.config.return_interim_results:
                    # Process buffered audio and send interim result
                    result = await session.process_buffered_audio()
                    if result["text"]:  # Only send if we have actual text
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "interim_result",
                                    "session_id": session_id,
                                    "is_final": False,
                                    **result,
                                }
                            )
                        )

            elif data["type"] == "finalize":
                if not session:
                    logger.warning("Received finalize without active session")
                    continue

                # Process any remaining audio and send final result
                result = await session.process_buffered_audio()

                # Send final result
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "final_result",
                            "session_id": session_id,
                            "is_final": True,
                            **result,
                        }
                    )
                )

                # Cleanup session
                if session_id in active_sessions:
                    del active_sessions[session_id]
                logger.info(f"Finalized session {session_id}")

            else:
                logger.warning(f"Unknown message type: {data['type']}")

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # Cleanup
        if session_id and session_id in active_sessions:
            del active_sessions[session_id]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parakeet ASR HTTP/WebSocket Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument("--model", help="NeMo model name", required=False)
    args = parser.parse_args()

    # Set model via environment variable
    if args.model:
        os.environ["PARAKEET_MODEL"] = args.model
    else:
        os.environ["PARAKEET_MODEL"] = "nvidia/parakeet-tdt-0.6b-v3"

    uvicorn.run(app, host=args.host, port=args.port)
