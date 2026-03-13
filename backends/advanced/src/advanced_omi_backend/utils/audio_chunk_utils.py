"""
Audio chunk utilities for Opus encoding/decoding and WAV reconstruction.

This module provides functions for:
- Converting PCM audio to Opus-compressed format
- Decoding Opus audio back to PCM
- Building complete WAV files from PCM data
- Retrieving audio chunks from MongoDB

All FFmpeg operations use subprocess with proper error handling and cleanup.
"""

import asyncio
import io
import logging
import tempfile
import time
import wave
from pathlib import Path
from typing import List, Optional

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument

logger = logging.getLogger(__name__)


async def encode_pcm_to_opus(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    bitrate: int = 24,
) -> bytes:
    """
    Encode raw PCM audio to Opus format using FFmpeg.

    Args:
        pcm_data: Raw PCM audio bytes (signed 16-bit little-endian)
        sample_rate: Sample rate in Hz (default: 16000)
        channels: Number of audio channels (default: 1 for mono)
        bitrate: Opus bitrate in kbps (default: 24 for speech)

    Returns:
        Opus-encoded audio bytes

    Raises:
        RuntimeError: If FFmpeg encoding fails

    Example:
        >>> pcm_bytes = b"..."  # 10 seconds of 16kHz mono PCM
        >>> opus_bytes = await encode_pcm_to_opus(pcm_bytes)
        >>> # opus_bytes is ~30KB vs 320KB PCM (94% reduction)
    """
    # Create temporary files for FFmpeg I/O
    with tempfile.NamedTemporaryFile(
        suffix=".pcm", delete=False
    ) as pcm_file, tempfile.NamedTemporaryFile(
        suffix=".opus", delete=False
    ) as opus_file:

        pcm_path = Path(pcm_file.name)
        opus_path = Path(opus_file.name)

        try:
            # Write PCM data to temp file
            pcm_file.write(pcm_data)
            pcm_file.flush()

            # FFmpeg command: PCM → Opus
            # -f s16le: signed 16-bit little-endian PCM
            # -ar: sample rate
            # -ac: audio channels
            # -c:a libopus: Opus encoder
            # -b:a: bitrate
            # -vbr on: variable bitrate for better quality
            # -application voip: optimize for speech
            cmd = [
                "ffmpeg",
                "-f",
                "s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-i",
                str(pcm_path),
                "-c:a",
                "libopus",
                "-b:a",
                f"{bitrate}k",
                "-vbr",
                "on",
                "-application",
                "voip",
                "-y",  # Overwrite output
                str(opus_path),
            ]

            # Run FFmpeg
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                logger.error(f"FFmpeg Opus encoding failed: {error_msg}")
                raise RuntimeError(f"Opus encoding failed: {error_msg}")

            # Read Opus output
            with open(opus_path, "rb") as f:
                opus_data = f.read()

            logger.debug(
                f"Encoded PCM ({len(pcm_data)} bytes) → Opus ({len(opus_data)} bytes), "
                f"compression ratio: {len(opus_data)/len(pcm_data):.3f}"
            )

            return opus_data

        finally:
            # Cleanup temporary files
            pcm_path.unlink(missing_ok=True)
            opus_path.unlink(missing_ok=True)


async def decode_opus_to_pcm(
    opus_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    """
    Decode Opus audio to raw PCM format using FFmpeg.

    Args:
        opus_data: Opus-encoded audio bytes
        sample_rate: Target sample rate in Hz (default: 16000)
        channels: Target number of channels (default: 1 for mono)

    Returns:
        Raw PCM audio bytes (signed 16-bit little-endian)

    Raises:
        RuntimeError: If FFmpeg decoding fails

    Example:
        >>> opus_bytes = b"..."  # Opus-encoded audio
        >>> pcm_bytes = await decode_opus_to_pcm(opus_bytes)
        >>> # pcm_bytes can be played or concatenated
    """
    # Create temporary files for FFmpeg I/O
    with tempfile.NamedTemporaryFile(
        suffix=".opus", delete=False
    ) as opus_file, tempfile.NamedTemporaryFile(
        suffix=".pcm", delete=False
    ) as pcm_file:

        opus_path = Path(opus_file.name)
        pcm_path = Path(pcm_file.name)

        try:
            # Write Opus data to temp file
            opus_file.write(opus_data)
            opus_file.flush()

            # FFmpeg command: Opus → PCM
            # -i: input Opus file
            # -f s16le: output as signed 16-bit little-endian PCM
            # -ar: resample to target sample rate
            # -ac: convert to target channel count
            cmd = [
                "ffmpeg",
                "-i",
                str(opus_path),
                "-f",
                "s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-y",  # Overwrite output
                str(pcm_path),
            ]

            # Run FFmpeg
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                logger.error(f"FFmpeg Opus decoding failed: {error_msg}")
                raise RuntimeError(f"Opus decoding failed: {error_msg}")

            # Read PCM output
            with open(pcm_path, "rb") as f:
                pcm_data = f.read()

            logger.debug(
                f"Decoded Opus ({len(opus_data)} bytes) → PCM ({len(pcm_data)} bytes)"
            )

            return pcm_data

        finally:
            # Cleanup temporary files
            opus_path.unlink(missing_ok=True)
            pcm_path.unlink(missing_ok=True)


async def build_wav_from_pcm(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """
    Build a complete WAV file from raw PCM data.

    Args:
        pcm_data: Raw PCM audio bytes (signed 16-bit little-endian)
        sample_rate: Sample rate in Hz (default: 16000)
        channels: Number of audio channels (default: 1 for mono)
        sample_width: Bytes per sample (default: 2 for 16-bit)

    Returns:
        Complete WAV file as bytes (including headers)

    Example:
        >>> pcm_bytes = b"..."  # Raw PCM audio
        >>> wav_bytes = await build_wav_from_pcm(pcm_bytes)
        >>> # wav_bytes can be served via StreamingResponse
    """
    # Use BytesIO as in-memory file
    wav_buffer = io.BytesIO()

    try:
        # Create WAV file writer
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)

        # Get WAV bytes
        wav_bytes = wav_buffer.getvalue()

        logger.debug(
            f"Built WAV file: {len(wav_bytes)} bytes "
            f"(PCM: {len(pcm_data)}, header: {len(wav_bytes) - len(pcm_data)})"
        )

        return wav_bytes

    finally:
        wav_buffer.close()


async def retrieve_audio_chunks(
    conversation_id: str,
    start_index: int = 0,
    limit: Optional[int] = None,
) -> List[AudioChunkDocument]:
    """
    Retrieve audio chunks from MongoDB for a conversation.

    Chunks are returned in sequential order by chunk_index.

    Args:
        conversation_id: Parent conversation ID
        start_index: First chunk index to retrieve (default: 0)
        limit: Maximum number of chunks to retrieve (default: None for all)

    Returns:
        List of AudioChunkDocument instances, sorted by chunk_index

    Example:
        >>> # Get all chunks for a conversation
        >>> chunks = await retrieve_audio_chunks("550e8400-e29b-41d4...")
        >>> # Get chunks 5-14 (10 chunks starting at index 5)
        >>> chunks = await retrieve_audio_chunks("550e8400-e29b-41d4...", start_index=5, limit=10)
    """
    # Build query
    query = AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conversation_id,
        AudioChunkDocument.chunk_index >= start_index,
    )

    # Apply limit if specified
    if limit is not None:
        query = query.limit(limit)

    # Execute query with sorting
    chunks = await query.sort("+chunk_index").to_list()

    logger.debug(
        f"Retrieved {len(chunks)} chunks for conversation {conversation_id[:8]}... "
        f"(start_index={start_index}, limit={limit})"
    )

    return chunks


async def concatenate_chunks_to_pcm(
    chunks: List[AudioChunkDocument],
) -> bytes:
    """
    Decode and concatenate multiple audio chunks into a single PCM buffer.

    Args:
        chunks: List of AudioChunkDocument instances (should be pre-sorted)

    Returns:
        Concatenated PCM audio bytes

    Example:
        >>> chunks = await retrieve_audio_chunks(conversation_id)
        >>> pcm_data = await concatenate_chunks_to_pcm(chunks)
        >>> wav_data = await build_wav_from_pcm(pcm_data)
    """
    if not chunks:
        return b""

    pcm_buffer = bytearray()

    for chunk in chunks:
        # Decode Opus → PCM
        pcm_data = await decode_opus_to_pcm(
            opus_data=chunk.audio_data,
            sample_rate=chunk.sample_rate,
            channels=chunk.channels,
        )

        # Append to buffer
        pcm_buffer.extend(pcm_data)

    logger.debug(f"Concatenated {len(chunks)} chunks → {len(pcm_buffer)} bytes PCM")

    return bytes(pcm_buffer)


async def reconstruct_wav_from_conversation(
    conversation_id: str,
    start_index: int = 0,
    limit: Optional[int] = None,
) -> bytes:
    """
    Reconstruct a complete WAV file from MongoDB chunks.

    This is a high-level convenience function that:
    1. Retrieves chunks from MongoDB
    2. Decodes Opus → PCM
    3. Concatenates PCM data
    4. Builds WAV file with headers

    Args:
        conversation_id: Parent conversation ID
        start_index: First chunk to include (default: 0)
        limit: Maximum chunks to include (default: None for all)

    Returns:
        Complete WAV file as bytes

    Raises:
        ValueError: If no chunks found for conversation

    Example:
        >>> # Get complete audio for conversation
        >>> wav_data = await reconstruct_wav_from_conversation(conversation_id)
        >>>
        >>> # Get first 60 seconds (6 chunks @ 10s each)
        >>> wav_data = await reconstruct_wav_from_conversation(conversation_id, limit=6)
    """
    # Retrieve chunks
    chunks = await retrieve_audio_chunks(
        conversation_id=conversation_id,
        start_index=start_index,
        limit=limit,
    )

    if not chunks:
        raise ValueError(f"No audio chunks found for conversation {conversation_id}")

    # Get audio format from first chunk
    sample_rate = chunks[0].sample_rate
    channels = chunks[0].channels

    # Decode and concatenate
    pcm_data = await concatenate_chunks_to_pcm(chunks)

    # Build WAV file
    wav_data = await build_wav_from_pcm(
        pcm_data=pcm_data,
        sample_rate=sample_rate,
        channels=channels,
    )

    logger.info(
        f"Reconstructed WAV for conversation {conversation_id[:8]}...: "
        f"{len(chunks)} chunks, {len(wav_data)} bytes, "
        f"{len(pcm_data) / sample_rate / channels / 2:.1f}s duration"
    )

    return wav_data


async def reconstruct_audio_segments(
    conversation_id: str,
    segment_duration: float = 900.0,  # 15 minutes
    overlap: float = 30.0,  # 30 seconds overlap for continuity
):
    """
    Reconstruct audio from MongoDB chunks in time-bounded segments.

    This function yields audio segments from a conversation, allowing
    processing of large files without loading everything into memory.

    Args:
        conversation_id: Parent conversation ID
        segment_duration: Duration of each segment in seconds (default: 900 = 15 minutes)
        overlap: Overlap between segments in seconds (default: 30)

    Yields:
        Tuple of (wav_bytes, start_time, end_time) for each segment

    Example:
        >>> # Process 73-minute conversation in 15-minute chunks
        >>> async for wav_data, start, end in reconstruct_audio_segments(conv_id):
        ...     # Process segment (only ~27 MB in memory at a time)
        ...     result = await process_segment(wav_data, start, end)

    Note:
        Overlap is added to all segments except the final one, to ensure
        speaker continuity across segment boundaries. Overlapping regions
        should be merged during post-processing.
    """
    from advanced_omi_backend.models.conversation import Conversation

    # Get conversation metadata
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise ValueError(f"Conversation {conversation_id} not found")

    total_duration = conversation.audio_total_duration or 0.0

    if total_duration == 0:
        logger.warning(
            f"Conversation {conversation_id} has zero duration, no segments to yield"
        )
        return

    # Get audio format from first chunk
    first_chunk = await AudioChunkDocument.find_one(
        AudioChunkDocument.conversation_id == conversation_id
    )

    if not first_chunk:
        raise ValueError(f"No audio chunks found for conversation {conversation_id}")

    sample_rate = first_chunk.sample_rate
    channels = first_chunk.channels

    # Calculate segment boundaries
    start_time = 0.0

    while start_time < total_duration:
        # Calculate segment end time with overlap
        end_time = min(start_time + segment_duration + overlap, total_duration)

        # Get chunks that overlap with this time range
        # Note: Using start_time and end_time fields from chunks
        chunks = (
            await AudioChunkDocument.find(
                AudioChunkDocument.conversation_id == conversation_id,
                AudioChunkDocument.start_time
                < end_time,  # Chunk starts before segment ends
                AudioChunkDocument.end_time
                > start_time,  # Chunk ends after segment starts
            )
            .sort(+AudioChunkDocument.chunk_index)
            .to_list()
        )

        if not chunks:
            logger.warning(
                f"No chunks found for time range {start_time:.1f}s - {end_time:.1f}s "
                f"in conversation {conversation_id[:8]}..."
            )
            start_time += segment_duration
            continue

        # Decode and concatenate chunks
        pcm_data = await concatenate_chunks_to_pcm(chunks)

        # Build WAV file for this segment
        wav_bytes = await build_wav_from_pcm(
            pcm_data=pcm_data,
            sample_rate=sample_rate,
            channels=channels,
        )

        logger.info(
            f"Yielding segment for {conversation_id[:8]}...: "
            f"{start_time:.1f}s - {end_time:.1f}s "
            f"({len(chunks)} chunks, {len(wav_bytes)} bytes)"
        )

        yield (wav_bytes, start_time, end_time)

        # Move to next segment (no overlap on the starting edge)
        start_time += segment_duration


async def reconstruct_audio_segment(
    conversation_id: str, start_time: float, end_time: float
) -> bytes:
    """
    Reconstruct audio for a specific time range from MongoDB chunks.

    This function returns a single audio segment for the specified time range,
    enabling on-demand access to conversation audio without loading the entire
    file into memory. Used by the audio segment API endpoint.

    Args:
        conversation_id: Conversation ID
        start_time: Start time in seconds
        end_time: End time in seconds

    Returns:
        WAV audio bytes (16kHz mono or original format)

    Raises:
        ValueError: If conversation not found or has no audio
        Exception: If audio reconstruction fails

    Example:
        >>> # Get first 60 seconds of audio
        >>> wav_bytes = await reconstruct_audio_segment(conv_id, 0.0, 60.0)
        >>> # Save to file
        >>> with open("segment.wav", "wb") as f:
        ...     f.write(wav_bytes)
    """
    start_timer = time.time()
    from advanced_omi_backend.models.conversation import Conversation

    # Validate start_time
    if start_time < 0:
        raise ValueError(f"start_time must be >= 0, got {start_time}")

    # Get conversation metadata
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise ValueError(f"Conversation {conversation_id} not found")

    total_duration = conversation.audio_total_duration or 0.0

    if total_duration == 0:
        raise ValueError(f"Conversation {conversation_id} has no audio")

    # Clamp values to valid ranges
    start_time = max(0, start_time)
    end_time = min(end_time, total_duration)

    # Validate clamped time range
    if end_time <= start_time:
        raise ValueError(
            f"Invalid time range: end_time ({end_time}s) must be > start_time ({start_time}s)"
        )

    # Get audio format from first chunk
    first_chunk = await AudioChunkDocument.find_one(
        AudioChunkDocument.conversation_id == conversation_id
    )

    if not first_chunk:
        raise ValueError(f"No audio chunks found for conversation {conversation_id}")

    sample_rate = first_chunk.sample_rate
    channels = first_chunk.channels

    # Get chunks that overlap with this time range
    chunks = (
        await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id,
            AudioChunkDocument.start_time
            < end_time,  # Chunk starts before segment ends
            AudioChunkDocument.end_time > start_time,  # Chunk ends after segment starts
        )
        .sort(+AudioChunkDocument.chunk_index)
        .to_list()
    )

    if not chunks:
        logger.warning(
            f"No chunks found for time range {start_time:.1f}s - {end_time:.1f}s "
            f"in conversation {conversation_id[:8]}..."
        )
        # Return silence for empty range
        return await build_wav_from_pcm(
            pcm_data=b"",
            sample_rate=sample_rate,
            channels=channels,
        )

    # Decode each chunk and clip to exact time boundaries for precise segment extraction
    pcm_buffer = bytearray()
    bytes_per_second = sample_rate * channels * 2  # 16-bit = 2 bytes per sample

    for chunk in chunks:
        # Decode this chunk to PCM
        pcm_data = await decode_opus_to_pcm(
            opus_data=chunk.audio_data,
            sample_rate=chunk.sample_rate,
            channels=chunk.channels,
        )

        # Calculate clip boundaries for this chunk
        clip_start_byte = 0
        clip_end_byte = len(pcm_data)

        # Trim start if chunk begins before requested start_time
        if chunk.start_time < start_time:
            offset_seconds = start_time - chunk.start_time
            offset_bytes = int(offset_seconds * bytes_per_second)
            # Align to sample boundary (2 bytes for 16-bit audio)
            clip_start_byte = (offset_bytes // 2) * 2

        # Trim end if chunk extends past requested end_time
        if chunk.end_time > end_time:
            # Calculate duration from chunk start to requested end
            duration_seconds = end_time - chunk.start_time
            duration_bytes = int(duration_seconds * bytes_per_second)
            # Align to sample boundary
            clip_end_byte = (duration_bytes // 2) * 2

        # Append only the clipped portion to buffer
        if clip_start_byte < clip_end_byte:
            clipped_pcm = pcm_data[clip_start_byte:clip_end_byte]
            pcm_buffer.extend(clipped_pcm)

            logger.debug(
                f"Chunk {chunk.chunk_index}: [{chunk.start_time:.1f}s - {chunk.end_time:.1f}s] "
                f"→ clipped [{max(chunk.start_time, start_time):.1f}s - {min(chunk.end_time, end_time):.1f}s] "
                f"({len(clipped_pcm)} bytes)"
            )

    # Build WAV file from precisely trimmed PCM data
    wav_bytes = await build_wav_from_pcm(
        pcm_data=bytes(pcm_buffer),
        sample_rate=sample_rate,
        channels=channels,
    )

    actual_duration = len(pcm_buffer) / bytes_per_second
    expected_duration = end_time - start_time
    processing_time = time.time() - start_timer

    logger.info(
        f"Reconstructed audio segment for {conversation_id[:8]}...: "
        f"{start_time:.1f}s - {end_time:.1f}s "
        f"({len(chunks)} chunks, {len(wav_bytes)} bytes WAV, "
        f"actual duration: {actual_duration:.2f}s, expected: {expected_duration:.2f}s, "
        f"processing time: {processing_time:.2f}s)"
    )

    return wav_bytes


def filter_transcript_by_time(
    transcript_data: dict, start_time: float, end_time: float
) -> dict:
    """
    Filter transcript data to only include words within a time range.

    Args:
        transcript_data: Dict with 'text' and 'words' keys
        start_time: Start time in seconds
        end_time: End time in seconds

    Returns:
        Filtered transcript data with only words in time range

    Example:
        >>> transcript = {"text": "full text", "words": [...100 words...]}
        >>> segment = filter_transcript_by_time(transcript, 0.0, 900.0)  # First 15 minutes
        >>> # segment contains only words from 0-900 seconds
    """
    if not transcript_data or "words" not in transcript_data:
        return transcript_data

    words = transcript_data.get("words", [])

    if not words:
        return transcript_data

    # Filter words by time range
    filtered_words = []
    for word in words:
        word_start = word.get("start", 0)
        word_end = word.get("end", 0)

        # Include word if it overlaps with the time range
        if word_start < end_time and word_end > start_time:
            filtered_words.append(word)

    # Rebuild text from filtered words
    filtered_text = " ".join(word.get("word", "") for word in filtered_words)

    return {"text": filtered_text, "words": filtered_words}


async def convert_audio_to_chunks(
    conversation_id: str,
    audio_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
    chunk_duration: float = 10.0,
) -> int:
    """
    Convert raw PCM audio directly to MongoDB chunks without disk intermediary.

    This is the preferred method as it avoids unnecessary disk I/O.
    Used for both WebSocket streaming and file uploads.

    Args:
        conversation_id: Conversation ID to associate chunks with
        audio_data: Raw PCM audio bytes (16-bit mono)
        sample_rate: Audio sample rate (default: 16000 Hz)
        channels: Number of channels (default: 1 = mono)
        sample_width: Bytes per sample (default: 2 = 16-bit)
        chunk_duration: Duration of each chunk in seconds (default: 10.0)

    Returns:
        Number of chunks created

    Raises:
        ValueError: If audio duration exceeds 2 hours

    Example:
        >>> # Convert from memory without disk write
        >>> num_chunks = await convert_audio_to_chunks(
        ...     conversation_id="550e8400-e29b-41d4...",
        ...     audio_data=pcm_bytes,
        ...     sample_rate=16000,
        ...     channels=1,
        ...     sample_width=2,
        ... )
        >>> print(f"Created {num_chunks} chunks")
    """
    from bson import Binary

    from advanced_omi_backend.models.conversation import Conversation

    logger.info(f"📦 Converting audio to MongoDB chunks: {len(audio_data)} bytes PCM")

    # Calculate audio duration and validate maximum limit
    bytes_per_second = sample_rate * sample_width * channels
    total_duration_seconds = len(audio_data) / bytes_per_second
    MAX_DURATION_SECONDS = 7200  # 2 hours (720 chunks @ 10s each)

    if total_duration_seconds > MAX_DURATION_SECONDS:
        raise ValueError(
            f"Audio duration ({total_duration_seconds:.1f}s) exceeds maximum allowed "
            f"({MAX_DURATION_SECONDS}s / 2 hours). Please split the file into smaller segments."
        )

    # Calculate chunk size in bytes
    chunk_size_bytes = int(chunk_duration * bytes_per_second)

    # Collect all chunks before batch insert
    chunks_to_insert = []
    chunk_index = 0
    total_original_size = 0
    total_compressed_size = 0
    offset = 0

    while offset < len(audio_data):
        # Extract chunk PCM data
        chunk_end = min(offset + chunk_size_bytes, len(audio_data))
        chunk_pcm = audio_data[offset:chunk_end]

        if len(chunk_pcm) == 0:
            break

        # Calculate chunk timing
        chunk_start_time = offset / bytes_per_second
        chunk_end_time = chunk_end / bytes_per_second
        chunk_duration_actual = (chunk_end - offset) / bytes_per_second

        # Encode to Opus
        opus_data = await encode_pcm_to_opus(
            pcm_data=chunk_pcm,
            sample_rate=sample_rate,
            channels=channels,
            bitrate=24,  # 24kbps for speech
        )

        # Create MongoDB document
        audio_chunk = AudioChunkDocument(
            conversation_id=conversation_id,
            chunk_index=chunk_index,
            audio_data=Binary(opus_data),
            original_size=len(chunk_pcm),
            compressed_size=len(opus_data),
            start_time=chunk_start_time,
            end_time=chunk_end_time,
            duration=chunk_duration_actual,
            sample_rate=sample_rate,
            channels=channels,
        )

        # Add to batch
        chunks_to_insert.append(audio_chunk)

        # Update stats
        total_original_size += len(chunk_pcm)
        total_compressed_size += len(opus_data)
        chunk_index += 1
        offset = chunk_end

        logger.debug(
            f"💾 Prepared chunk {chunk_index}: "
            f"{len(chunk_pcm)} → {len(opus_data)} bytes"
        )

    # Batch insert all chunks to MongoDB (single database operation)
    if chunks_to_insert:
        await AudioChunkDocument.insert_many(chunks_to_insert)
        logger.info(
            f"✅ Batch inserted {len(chunks_to_insert)} chunks to MongoDB "
            f"({total_duration_seconds:.1f}s audio)"
        )

    # Update conversation metadata
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if conversation:
        compression_ratio = (
            total_compressed_size / total_original_size
            if total_original_size > 0
            else 0.0
        )

        logger.info(
            f"🔍 DEBUG: Setting metadata - chunks={chunk_index}, duration={total_duration_seconds:.2f}s, ratio={compression_ratio:.3f}"
        )

        conversation.audio_chunks_count = chunk_index
        conversation.audio_total_duration = total_duration_seconds
        conversation.audio_compression_ratio = compression_ratio

        logger.info(
            f"🔍 DEBUG: Before save - chunks={conversation.audio_chunks_count}, duration={conversation.audio_total_duration}"
        )
        await conversation.save()
        logger.info(f"🔍 DEBUG: After save - metadata should be persisted")
    else:
        logger.error(
            f"❌ Conversation {conversation_id} not found for metadata update!"
        )

    logger.info(
        f"✅ Converted audio to {chunk_index} MongoDB chunks: "
        f"{total_original_size / 1024 / 1024:.2f} MB → "
        f"{total_compressed_size / 1024 / 1024:.2f} MB "
        f"(compression: {compression_ratio:.3f}, "
        f"{(1 - compression_ratio) * 100:.1f}% savings)"
    )

    return chunk_index


async def convert_wav_to_chunks(
    conversation_id: str,
    wav_file_path: Path,
    chunk_duration: float = 10.0,
) -> int:
    """
    Convert an existing WAV file to MongoDB audio chunks.

    DEPRECATED: Use convert_audio_to_chunks() instead to avoid disk I/O.

    Used for uploaded audio files to ensure consistency with streaming audio storage.
    Reads WAV file, splits into 10-second chunks, encodes to Opus, and stores in MongoDB.

    Args:
        conversation_id: Conversation ID to associate chunks with
        wav_file_path: Path to existing WAV file
        chunk_duration: Duration of each chunk in seconds (default: 10.0)

    Returns:
        Number of chunks created

    Raises:
        FileNotFoundError: If WAV file doesn't exist
        ValueError: If WAV file is invalid or exceeds 2 hours

    Example:
        >>> # Convert uploaded file to chunks
        >>> num_chunks = await convert_wav_to_chunks(
        ...     conversation_id="550e8400-e29b-41d4...",
        ...     wav_file_path=Path("/path/to/uploaded.wav")
        ... )
        >>> print(f"Created {num_chunks} chunks")
    """
    if not wav_file_path.exists():
        raise FileNotFoundError(f"WAV file not found: {wav_file_path}")

    from bson import Binary

    from advanced_omi_backend.models.conversation import Conversation

    logger.info(f"📦 Converting WAV file to MongoDB chunks: {wav_file_path}")

    # Read WAV file
    import wave

    with wave.open(str(wav_file_path), "rb") as wav:
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        total_frames = wav.getnframes()

        # Read all PCM data
        pcm_data = wav.readframes(total_frames)

    logger.info(
        f"📁 Read WAV: {len(pcm_data)} bytes PCM, "
        f"{sample_rate}Hz, {channels}ch, {sample_width*8}-bit"
    )

    # Calculate audio duration and validate maximum limit
    bytes_per_second = sample_rate * sample_width * channels
    total_duration_seconds = len(pcm_data) / bytes_per_second
    MAX_DURATION_SECONDS = 7200  # 2 hours (720 chunks @ 10s each)

    if total_duration_seconds > MAX_DURATION_SECONDS:
        raise ValueError(
            f"Audio duration ({total_duration_seconds:.1f}s) exceeds maximum allowed "
            f"({MAX_DURATION_SECONDS}s / 2 hours). Please split the file into smaller segments."
        )

    # Calculate chunk size in bytes
    chunk_size_bytes = int(chunk_duration * bytes_per_second)

    # Collect all chunks before batch insert
    chunks_to_insert = []
    chunk_index = 0
    total_original_size = 0
    total_compressed_size = 0
    offset = 0

    while offset < len(pcm_data):
        # Extract chunk PCM data
        chunk_end = min(offset + chunk_size_bytes, len(pcm_data))
        chunk_pcm = pcm_data[offset:chunk_end]

        if len(chunk_pcm) == 0:
            break

        # Calculate chunk timing
        chunk_start_time = offset / bytes_per_second
        chunk_end_time = chunk_end / bytes_per_second
        chunk_duration_actual = (chunk_end - offset) / bytes_per_second

        # Encode to Opus
        opus_data = await encode_pcm_to_opus(
            pcm_data=chunk_pcm,
            sample_rate=sample_rate,
            channels=channels,
            bitrate=24,  # 24kbps for speech
        )

        # Create MongoDB document
        audio_chunk = AudioChunkDocument(
            conversation_id=conversation_id,
            chunk_index=chunk_index,
            audio_data=Binary(opus_data),
            original_size=len(chunk_pcm),
            compressed_size=len(opus_data),
            start_time=chunk_start_time,
            end_time=chunk_end_time,
            duration=chunk_duration_actual,
            sample_rate=sample_rate,
            channels=channels,
        )

        # Add to batch
        chunks_to_insert.append(audio_chunk)

        # Update stats
        total_original_size += len(chunk_pcm)
        total_compressed_size += len(opus_data)
        chunk_index += 1
        offset = chunk_end

        logger.debug(
            f"💾 Prepared chunk {chunk_index}: "
            f"{len(chunk_pcm)} → {len(opus_data)} bytes"
        )

    # Batch insert all chunks to MongoDB (single database operation)
    if chunks_to_insert:
        await AudioChunkDocument.insert_many(chunks_to_insert)
        logger.info(
            f"✅ Batch inserted {len(chunks_to_insert)} chunks to MongoDB "
            f"({total_duration_seconds:.1f}s audio)"
        )

    # Update conversation metadata
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if conversation:
        compression_ratio = (
            total_compressed_size / total_original_size
            if total_original_size > 0
            else 0.0
        )

        logger.info(
            f"🔍 DEBUG: Setting metadata - chunks={chunk_index}, duration={total_duration_seconds:.2f}s, ratio={compression_ratio:.3f}"
        )

        conversation.audio_chunks_count = chunk_index
        conversation.audio_total_duration = total_duration_seconds
        conversation.audio_compression_ratio = compression_ratio

        logger.info(
            f"🔍 DEBUG: Before save - chunks={conversation.audio_chunks_count}, duration={conversation.audio_total_duration}"
        )
        await conversation.save()
        logger.info(f"🔍 DEBUG: After save - metadata should be persisted")
    else:
        logger.error(
            f"❌ Conversation {conversation_id} not found for metadata update!"
        )

    logger.info(
        f"✅ Converted WAV to {chunk_index} MongoDB chunks: "
        f"{total_original_size / 1024 / 1024:.2f} MB → "
        f"{total_compressed_size / 1024 / 1024:.2f} MB "
        f"(compression: {compression_ratio:.3f}, "
        f"{(1 - compression_ratio) * 100:.1f}% savings)"
    )

    return chunk_index


async def wait_for_audio_chunks(
    conversation_id: str,
    max_wait_seconds: int = 30,
    min_chunks: int = 1,
) -> bool:
    """
    Wait for MongoDB audio chunks to be available for a conversation.

    Replaces wait_for_audio_file() for MongoDB-based storage.
    Polls MongoDB until chunks exist or timeout occurs.

    Args:
        conversation_id: Conversation ID to check
        max_wait_seconds: Maximum wait time in seconds (default: 30)
        min_chunks: Minimum number of chunks required (default: 1)

    Returns:
        True if chunks are available, False if timeout

    Example:
        >>> # Wait for chunks before transcription
        >>> if await wait_for_audio_chunks(conversation_id):
        ...     await transcribe_full_audio_job(...)
        ... else:
        ...     logger.error("No audio chunks available")
    """
    import asyncio
    import time

    wait_start = time.time()

    while time.time() - wait_start < max_wait_seconds:
        # Query chunk count
        chunks = await retrieve_audio_chunks(
            conversation_id=conversation_id,
            start_index=0,
            limit=1,  # Just check if any exist
        )

        if len(chunks) >= min_chunks:
            wait_duration = time.time() - wait_start
            logger.info(
                f"✅ Audio chunks ready for conversation {conversation_id[:12]} "
                f"after {wait_duration:.1f}s ({len(chunks)} chunks found)"
            )
            return True

        # Log progress every 5 seconds
        elapsed = time.time() - wait_start
        if int(elapsed) % 5 == 0 and int(elapsed) > 0:
            logger.info(
                f"⏳ Waiting for audio chunks (conversation {conversation_id[:12]})... "
                f"({elapsed:.0f}s elapsed)"
            )

        await asyncio.sleep(0.5)  # Check every 500ms

    logger.error(
        f"❌ Audio chunks not found after {max_wait_seconds}s "
        f"(conversation: {conversation_id[:12]})"
    )
    return False
