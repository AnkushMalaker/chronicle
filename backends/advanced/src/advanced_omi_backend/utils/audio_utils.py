###############################################################################
# AUDIO PROCESSING FUNCTIONS
###############################################################################

import asyncio
import logging
import os
import time
import uuid as uuid_lib
from pathlib import Path

# Type import to avoid circular imports
from typing import TYPE_CHECKING, Optional

from wyoming.audio import AudioChunk

if TYPE_CHECKING:
    from advanced_omi_backend.client import ClientState

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")

# Import constants from main.py (these are defined there)
MIN_SPEECH_SEGMENT_DURATION = float(os.getenv("MIN_SPEECH_SEGMENT_DURATION", "1.0"))  # seconds
CROPPING_CONTEXT_PADDING = float(os.getenv("CROPPING_CONTEXT_PADDING", "0.1"))  # seconds


class AudioValidationError(Exception):
    """Exception raised when audio validation fails."""
    pass


async def validate_and_prepare_audio(
    audio_data: bytes,
    expected_sample_rate: int = 16000,
    convert_to_mono: bool = True
) -> tuple[bytes, int, int, int, float]:
    """
    Validate WAV audio data and prepare it for processing.

    Args:
        audio_data: Raw WAV file bytes
        expected_sample_rate: Expected sample rate (default: 16000 Hz)
        convert_to_mono: Whether to convert stereo to mono (default: True)

    Returns:
        Tuple of (processed_audio_data, sample_rate, sample_width, channels, duration)

    Raises:
        AudioValidationError: If audio validation fails
    """
    import io
    import wave
    import numpy as np

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

    # Validate sample rate
    if sample_rate != expected_sample_rate:
        raise AudioValidationError(
            f"Sample rate must be {expected_sample_rate}Hz, got {sample_rate}Hz"
        )

    # Convert stereo to mono if requested
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
    from advanced_omi_backend.models.audio_file import AudioFile

    # Validate and prepare audio if needed
    if validate:
        audio_data, sample_rate, sample_width, channels, duration = \
            await validate_and_prepare_audio(raw_audio_data)
    else:
        # For WebSocket path - audio is already processed PCM
        audio_data = raw_audio_data
        sample_rate = 16000  # WebSocket always uses 16kHz
        sample_width = 2
        channels = 1
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

    # Create AudioFile database entry using Beanie model
    audio_file = AudioFile(
        audio_uuid=audio_uuid,
        source=source,
        audio_path=wav_filename,
        client_id=client_id,
        timestamp=timestamp,
        user_id=user_id,
        user_email=user_email,
        has_speech=False,  # Will be updated by transcription
        speech_analysis={}, 
    )
    await audio_file.insert()

    audio_logger.info(f"✅ Created AudioFile entry for {audio_uuid}")

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


async def _process_audio_cropping_with_relative_timestamps(
    original_path: str,
    speech_segments: list[tuple[float, float]],
    output_path: str,
    audio_uuid: str,
    _deprecated_chunk_repo=None,  # Deprecated - kept for backward compatibility
) -> tuple[bool, list[dict]]:
    """
    Process audio cropping with speech segments already in relative format.

    The segments are expected to be in relative format (seconds from audio start),
    as provided by Deepgram transcription. No timestamp conversion is needed.

    Note: Database updates are now handled by the caller (audio_jobs.py).

    Returns:
        Tuple of (success: bool, segment_mapping: list[dict])
    """
    try:
        # Validate input segments
        validated_segments = []
        for start_rel, end_rel in speech_segments:
            # Validate input timestamps
            if start_rel >= end_rel:
                logger.warning(
                    f"⚠️ Invalid speech segment: start={start_rel} >= end={end_rel}, skipping"
                )
                continue

            # Ensure timestamps are positive (sanity check)
            if start_rel < 0:
                logger.warning(
                    f"⚠️ Negative start timestamp: {start_rel}, clamping to 0.0"
                )
                start_rel = 0.0
            if end_rel < 0:
                logger.warning(
                    f"⚠️ Negative end timestamp: {end_rel}, skipping segment"
                )
                continue

            validated_segments.append((start_rel, end_rel))

        logger.info(f"🕐 Processing cropping for {audio_uuid}")
        logger.info(f"🕐 Input segments (relative timestamps): {speech_segments}")
        logger.info(f"🕐 Validated segments: {validated_segments}")

        # Validate that we have valid segments
        if not validated_segments:
            logger.warning(
                f"No valid segments for cropping {audio_uuid}"
            )
            return False, []

        success, segment_mapping = await _crop_audio_with_ffmpeg(original_path, validated_segments, output_path)
        if success:
            cropped_filename = output_path.split("/")[-1]
            logger.info(f"Successfully processed cropped audio: {cropped_filename}")
            return True, segment_mapping
        else:
            logger.error(f"Failed to crop audio for {audio_uuid}")
            return False, segment_mapping
    except Exception as e:
        logger.error(f"Error in audio cropping task for {audio_uuid}: {e}", exc_info=True)
        return False, []


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
        logger.exception(f"❌ Failed to write PCM to WAV")
        raise


async def _crop_audio_with_ffmpeg(
    original_path: str, speech_segments: list[tuple[float, float]], output_path: str
) -> tuple[bool, list[dict]]:
    """
    Use ffmpeg to crop audio - runs as async subprocess, no GIL issues.

    Returns:
        Tuple of (success: bool, segment_mapping: list[dict])

        segment_mapping contains one entry per input segment with:
        - original_index: Index in input speech_segments
        - original_start/end: Original timestamps in source audio
        - cropped_start/end: Where the speech starts/ends in cropped file (None if filtered)
        - kept: Whether segment was kept (True) or filtered out (False)
    """
    logger.info(f"Cropping audio {original_path} with {len(speech_segments)} speech segments")

    if not speech_segments:
        logger.warning(f"No speech segments to crop for {original_path}")
        return False, []

    # Check if the original file exists
    if not os.path.exists(original_path):
        logger.error(f"Original audio file does not exist: {original_path}")
        return False, []

    # Filter out segments that are too short and build mapping
    filtered_segments = []
    segment_mapping = []
    current_cropped_offset = 0.0

    for idx, (start, end) in enumerate(speech_segments):
        duration = end - start
        if duration >= MIN_SPEECH_SEGMENT_DURATION:
            # Add padding around speech segments
            padded_start = max(0, start - CROPPING_CONTEXT_PADDING)
            padded_end = end + CROPPING_CONTEXT_PADDING
            padded_duration = padded_end - padded_start

            filtered_segments.append((padded_start, padded_end))

            # Calculate where the speech (not padding) appears in cropped file
            # The cropped file will have: [padding_before][speech][padding_after]
            padding_before = start - padded_start
            speech_start_in_cropped = current_cropped_offset + padding_before
            speech_end_in_cropped = speech_start_in_cropped + duration

            segment_mapping.append({
                "original_index": idx,
                "original_start": start,
                "original_end": end,
                "cropped_start": speech_start_in_cropped,
                "cropped_end": speech_end_in_cropped,
                "kept": True
            })

            # Move offset by the full padded duration
            current_cropped_offset += padded_duration
        else:
            # Segment filtered out
            segment_mapping.append({
                "original_index": idx,
                "original_start": start,
                "original_end": end,
                "cropped_start": None,
                "cropped_end": None,
                "kept": False
            })
            logger.debug(
                f"Skipping short segment: {start}-{end} ({duration:.2f}s < {MIN_SPEECH_SEGMENT_DURATION}s)"
            )

    if not filtered_segments:
        logger.warning(
            f"No segments meet minimum duration ({MIN_SPEECH_SEGMENT_DURATION}s) for {original_path}"
        )
        return False, segment_mapping

    logger.info(
        f"Cropping audio {original_path} with {len(filtered_segments)} speech segments (filtered from {len(speech_segments)})"
    )

    try:
        # Build ffmpeg filter for concatenating speech segments
        filter_parts = []
        for i, (start, end) in enumerate(filtered_segments):
            duration = end - start
            filter_parts.append(
                f"[0:a]atrim=start={start}:duration={duration},asetpts=PTS-STARTPTS[seg{i}]"
            )

        # Concatenate all segments
        inputs = "".join(f"[seg{i}]" for i in range(len(filtered_segments)))
        concat_filter = f"{inputs}concat=n={len(filtered_segments)}:v=0:a=1[out]"

        full_filter = ";".join(filter_parts + [concat_filter])

        # Run ffmpeg as async subprocess
        cmd = [
            "ffmpeg",
            "-y",  # -y = overwrite output
            "-i",
            original_path,
            "-filter_complex",
            full_filter,
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",  # Keep same format as original
            output_path,
        ]

        logger.info(f"Running ffmpeg command: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()
        if stdout:
            logger.debug(f"FFMPEG stdout: {stdout.decode()}")

        if process.returncode == 0:
            # Calculate cropped duration
            cropped_duration = sum(end - start for start, end in filtered_segments)
            logger.info(
                f"Successfully cropped {original_path} -> {output_path} ({cropped_duration:.1f}s from {len(filtered_segments)} segments)"
            )
            return True, segment_mapping
        else:
            error_msg = stderr.decode() if stderr else "Unknown ffmpeg error"
            logger.error(f"ffmpeg failed for {original_path}: {error_msg}")
            return False, segment_mapping

    except Exception as e:
        logger.error(f"Error running ffmpeg on {original_path}: {e}", exc_info=True)
        return False, segment_mapping
