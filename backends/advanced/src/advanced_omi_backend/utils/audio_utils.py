###############################################################################
# AUDIO PROCESSING FUNCTIONS
###############################################################################

import asyncio
import io
import logging
import os
import time
import uuid as uuid_lib
import wave
from pathlib import Path

# Type import to avoid circular imports
from typing import TYPE_CHECKING, Optional

import numpy as np
from wyoming.audio import AudioChunk

if TYPE_CHECKING:
    from advanced_omi_backend.client import ClientState

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")

# Import constants from main.py (these are defined there)
MIN_SPEECH_SEGMENT_DURATION = float(os.getenv("MIN_SPEECH_SEGMENT_DURATION", "1.0"))  # seconds
CROPPING_CONTEXT_PADDING = float(os.getenv("CROPPING_CONTEXT_PADDING", "0.1"))  # seconds

SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".mp4", ".m4a", ".flac", ".ogg", ".webm"}
VIDEO_EXTENSIONS = {".mp4", ".webm"}


class AudioValidationError(Exception):
    """Exception raised when audio validation fails."""
    pass


async def resample_audio_with_ffmpeg(
    audio_data: bytes,
    input_sample_rate: int,
    input_channels: int,
    input_sample_width: int,
    target_sample_rate: int,
    target_channels: int = 1
) -> bytes:
    """
    Resample audio using FFmpeg with stdin/stdout pipes (no disk I/O).

    Args:
        audio_data: Raw PCM audio bytes
        input_sample_rate: Input sample rate in Hz
        input_channels: Number of input channels
        input_sample_width: Input sample width in bytes (2 for 16-bit, 4 for 32-bit)
        target_sample_rate: Target sample rate in Hz
        target_channels: Target number of channels (default: 1 for mono)

    Returns:
        Resampled PCM audio bytes (16-bit signed little-endian)

    Raises:
        RuntimeError: If FFmpeg resampling fails
    """
    # Determine FFmpeg format based on sample width
    if input_sample_width == 2:
        input_format = "s16le"  # 16-bit signed little-endian
    elif input_sample_width == 4:
        input_format = "s32le"  # 32-bit signed little-endian
    else:
        raise AudioValidationError(
            f"Unsupported sample width: {input_sample_width} bytes (only 2 or 4 supported)"
        )

    # FFmpeg command for resampling via pipes
    # pipe:0 = stdin, pipe:1 = stdout
    cmd = [
        "ffmpeg",
        "-f", input_format,
        "-ar", str(input_sample_rate),
        "-ac", str(input_channels),
        "-i", "pipe:0",  # Read from stdin
        "-ar", str(target_sample_rate),
        "-ac", str(target_channels),
        "-f", "s16le",  # Always output 16-bit
        "pipe:1",  # Write to stdout
    ]

    # Run FFmpeg with piped I/O
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Send input data and get output
    stdout, stderr = await process.communicate(input=audio_data)

    if process.returncode != 0:
        error_msg = stderr.decode() if stderr else "Unknown error"
        audio_logger.error(f"FFmpeg resampling failed: {error_msg}")
        raise RuntimeError(f"Audio resampling failed: {error_msg}")

    audio_logger.info(
        f"Resampled audio: {input_sample_rate}Hz/{input_channels}ch → "
        f"{target_sample_rate}Hz/{target_channels}ch "
        f"({len(audio_data)} → {len(stdout)} bytes)"
    )

    return stdout


async def convert_any_to_wav(file_data: bytes, file_extension: str) -> bytes:
    """
    Convert any supported audio/video file to 16kHz mono WAV using FFmpeg.

    For .wav input, returns the data as-is.
    For everything else, runs FFmpeg to extract audio and convert to WAV.

    Args:
        file_data: Raw file bytes
        file_extension: File extension including dot (e.g. ".mp3", ".mp4")

    Returns:
        WAV file bytes (16kHz, mono, 16-bit PCM)

    Raises:
        AudioValidationError: If FFmpeg conversion fails
    """
    ext = file_extension.lower()
    if ext == ".wav":
        return file_data

    cmd = [
        "ffmpeg",
        "-i", "pipe:0",
        "-vn",  # Strip video track (no-op for audio-only files)
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-f", "wav",
        "pipe:1",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=file_data)

    if process.returncode != 0:
        error_msg = stderr.decode() if stderr else "Unknown error"
        audio_logger.error(f"FFmpeg conversion failed for {ext}: {error_msg}")
        raise AudioValidationError(f"Failed to convert {ext} file to WAV: {error_msg}")

    audio_logger.info(
        f"Converted {ext} to WAV: {len(file_data)} → {len(stdout)} bytes"
    )

    return stdout


async def validate_and_prepare_audio(
    audio_data: bytes,
    expected_sample_rate: int = 16000,
    convert_to_mono: bool = True,
    auto_resample: bool = False
) -> tuple[bytes, int, int, int, float]:
    """
    Validate WAV audio data and prepare it for processing.

    Args:
        audio_data: Raw WAV file bytes
        expected_sample_rate: Expected sample rate (default: 16000 Hz)
        convert_to_mono: Whether to convert stereo to mono (default: True)
        auto_resample: Whether to automatically resample audio if sample rate doesn't match (default: False)

    Returns:
        Tuple of (processed_audio_data, sample_rate, sample_width, channels, duration)

    Raises:
        AudioValidationError: If audio validation fails
    """
    try:
        # Parse WAV file
        with wave.open(io.BytesIO(audio_data), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            channels = wav_file.getnchannels()
            frame_count = wav_file.getnframes()
            duration = frame_count / sample_rate if sample_rate > 0 else 0

            # Read audio data
            processed_audio = wav_file.readframes(frame_count)

    except Exception as e:
        raise AudioValidationError(f"Invalid WAV file: {str(e)}")

    # Handle sample rate mismatch
    if sample_rate != expected_sample_rate:
        if auto_resample:
            audio_logger.info(
                f"Auto-resampling audio from {sample_rate}Hz to {expected_sample_rate}Hz"
            )
            # Resample audio using FFmpeg (with pipes, no disk I/O)
            processed_audio = await resample_audio_with_ffmpeg(
                audio_data=processed_audio,
                input_sample_rate=sample_rate,
                input_channels=channels,
                input_sample_width=sample_width,
                target_sample_rate=expected_sample_rate,
                target_channels=1 if convert_to_mono else channels
            )
            # Update metadata after resampling
            sample_rate = expected_sample_rate
            sample_width = 2  # FFmpeg outputs 16-bit
            if convert_to_mono:
                channels = 1
            # Recalculate duration
            duration = len(processed_audio) / (sample_rate * sample_width * channels)
            # Skip stereo-to-mono conversion since resampling already handled it
            convert_to_mono = False
        else:
            raise AudioValidationError(
                f"Sample rate must be {expected_sample_rate}Hz, got {sample_rate}Hz"
            )

    # Convert stereo to mono if requested and not already done
    if convert_to_mono and channels == 2:
        audio_logger.info(f"Converting stereo audio to mono")

        if sample_width == 2:
            audio_array = np.frombuffer(processed_audio, dtype=np.int16)
        elif sample_width == 4:
            audio_array = np.frombuffer(processed_audio, dtype=np.int32)
        else:
            raise AudioValidationError(
                f"Unsupported sample width for stereo conversion: {sample_width} bytes"
            )

        # Reshape to separate channels and average
        audio_array = audio_array.reshape(-1, 2)
        processed_audio = np.mean(audio_array, axis=1).astype(audio_array.dtype).tobytes()
        channels = 1

    audio_logger.debug(
        f"Audio validated: {duration:.1f}s, {sample_rate}Hz, {channels}ch, {sample_width} bytes/sample"
    )

    return processed_audio, sample_rate, sample_width, channels, duration


async def write_audio_file(
    raw_audio_data: bytes,
    audio_uuid: str,
    source: str,
    client_id: str,
    user_id: str,
    user_email: str,
    timestamp: int,
    chunk_dir: Optional[Path] = None,
    validate: bool = True,
    pcm_sample_rate: int = 16000,
    pcm_channels: int = 1,
    pcm_sample_width: int = 2,
) -> tuple[str, str, float]:
    """
    Validate, write audio data to WAV file, and create AudioSession database entry.

    This is shared logic used by both upload and WebSocket streaming paths.
    Handles validation, stereo→mono conversion, and database entry creation.

    Args:
        raw_audio_data: Raw audio bytes (WAV format if validate=True, or PCM if validate=False)
        audio_uuid: Unique identifier for this audio
        client_id: Client identifier
        user_id: User ID
        user_email: User email
        timestamp: Timestamp in milliseconds
        chunk_dir: Optional directory path (defaults to CHUNK_DIR from config)
        validate: Whether to validate and prepare audio (default: True for uploads, False for WebSocket)
        pcm_sample_rate: Sample rate for raw PCM data when validate=False (default: 16000)
        pcm_channels: Channel count for raw PCM data when validate=False (default: 1)
        pcm_sample_width: Sample width in bytes for raw PCM data when validate=False (default: 2)

    Returns:
        Tuple of (relative_audio_path, absolute_file_path, duration)
        - relative_audio_path: Path for database storage (e.g., "fixtures/123_abc_uuid.wav" or "123_abc_uuid.wav")
        - absolute_file_path: Full filesystem path for immediate file operations
        - duration: Audio duration in seconds

    Raises:
        AudioValidationError: If validation fails (when validate=True)
    """
    from easy_audio_interfaces.filesystem.filesystem_interfaces import LocalFileSink

    from advanced_omi_backend.config import CHUNK_DIR

    # Validate and prepare audio if needed
    if validate:
        audio_data, sample_rate, sample_width, channels, duration = \
            await validate_and_prepare_audio(raw_audio_data)
    else:
        # For WebSocket/streaming path - audio is already processed PCM
        audio_data = raw_audio_data
        sample_rate = pcm_sample_rate
        sample_width = pcm_sample_width
        channels = pcm_channels
        duration = len(audio_data) / (sample_rate * sample_width * channels)

    # Use provided chunk_dir or default from config
    output_dir = chunk_dir or CHUNK_DIR

    # Ensure directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create filename
    wav_filename = f"{timestamp}_{client_id}_{audio_uuid}.wav"
    file_path = output_dir / wav_filename

    # Calculate relative path for database storage
    # If output_dir is a subdirectory of CHUNK_DIR, include the folder prefix
    try:
        relative_path_parts = output_dir.relative_to(CHUNK_DIR)
        if str(relative_path_parts) != '.':
            relative_audio_path = f"{relative_path_parts}/{wav_filename}"
        else:
            relative_audio_path = wav_filename
    except ValueError:
        # output_dir is not relative to CHUNK_DIR, just use filename
        relative_audio_path = wav_filename

    # Create file sink and write audio
    sink = LocalFileSink(
        file_path=str(file_path),
        sample_rate=int(sample_rate),
        channels=int(channels),
        sample_width=int(sample_width)
    )

    await sink.open()
    audio_chunk = AudioChunk(
        rate=sample_rate,
        width=sample_width,
        channels=channels,
        audio=audio_data
    )
    await sink.write(audio_chunk)
    await sink.close()

    audio_logger.info(
        f"✅ Wrote audio file: {wav_filename} ({len(audio_data)} bytes, {duration:.1f}s)"
    )

    return relative_audio_path, str(file_path), duration


async def process_audio_chunk(
    audio_data: bytes,
    client_id: str,
    user_id: str,
    user_email: str,
    audio_format: dict,
    client_state: Optional["ClientState"] = None
) -> None:
    """Process a single audio chunk through Redis Streams pipeline.

    This function encapsulates the common pattern used across all audio input sources:
    1. Create AudioChunk with format details
    2. Publish to Redis Streams for distributed processing
    3. Update client state if provided

    Args:
        audio_data: Raw audio bytes
        client_id: Client identifier
        user_id: User identifier
        user_email: User email
        audio_format: Dict containing {rate, width, channels, timestamp}
        client_state: Optional ClientState for state updates
    """

    from advanced_omi_backend.services.audio_service import get_audio_stream_service

    # Extract format details
    rate = audio_format.get("rate", 16000)
    width = audio_format.get("width", 2)
    channels = audio_format.get("channels", 1)
    timestamp = audio_format.get("timestamp")

    # Use current time if no timestamp provided
    if timestamp is None:
        timestamp = int(time.time() * 1000)

    # Create AudioChunk with format details
    chunk = AudioChunk(
        audio=audio_data,
        rate=rate,
        width=width,
        channels=channels,
        timestamp=timestamp
    )

    # Publish audio chunk to Redis Streams
    audio_service = get_audio_stream_service()
    await audio_service.publish_audio_chunk(
        client_id=client_id,
        user_id=user_id,
        user_email=user_email,
        audio_chunk=chunk,
        audio_uuid=None,  # Will be generated by worker
        timestamp=timestamp
    )

    # Update client state if provided
    if client_state is not None:
        client_state.update_audio_received(chunk)


def pcm_to_wav_bytes(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2
) -> bytes:
    """
    Convert raw PCM audio data to WAV format in memory.

    Args:
        pcm_data: Raw PCM audio bytes
        sample_rate: Sample rate in Hz (default: 16000)
        channels: Number of audio channels (default: 1 for mono)
        sample_width: Sample width in bytes (default: 2 for 16-bit)

    Returns:
        WAV file data as bytes
    """
    import io
    import wave

    logger.debug(
        f"Converting PCM to WAV in memory: {len(pcm_data)} bytes "
        f"(rate={sample_rate}, channels={channels}, width={sample_width})"
    )

    # Use BytesIO to create WAV in memory
    wav_buffer = io.BytesIO()

    with wave.open(wav_buffer, 'wb') as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)

    # Get the WAV bytes
    wav_bytes = wav_buffer.getvalue()

    logger.debug(f"Created WAV in memory: {len(wav_bytes)} bytes")

    return wav_bytes


def write_pcm_to_wav(
    pcm_data: bytes,
    output_path: str,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2
) -> None:
    """
    Write raw PCM audio data to a WAV file.

    Args:
        pcm_data: Raw PCM audio bytes
        output_path: Path to output WAV file
        sample_rate: Sample rate in Hz (default: 16000)
        channels: Number of audio channels (default: 1 for mono)
        sample_width: Sample width in bytes (default: 2 for 16-bit)
    """
    import wave

    logger.info(
        f"Writing PCM to WAV: {len(pcm_data)} bytes -> {output_path} "
        f"(rate={sample_rate}, channels={channels}, width={sample_width})"
    )

    try:
        with wave.open(output_path, 'wb') as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)

        # Verify file was created
        file_size = os.path.getsize(output_path)
        duration = len(pcm_data) / (sample_rate * channels * sample_width)
        logger.info(
            f"✅ WAV file created: {output_path} ({file_size} bytes, {duration:.2f}s)"
        )

    except Exception as e:
        logger.error(f"❌ Failed to write PCM to WAV: {e}")
        raise
