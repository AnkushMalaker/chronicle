"""
Audio chunk utilities for Opus encoding/decoding and WAV reconstruction.

This module provides functions for:
- Converting PCM audio to Opus-compressed format
- Decoding Opus audio back to PCM
- Building complete WAV files from PCM data
- Retrieving audio chunks from MongoDB

All FFmpeg operations use subprocess with proper error handling and cleanup.
"""

import array
import asyncio
import io
import logging
import math
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from bson import Binary

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.waveform import WaveformData

logger = logging.getLogger(__name__)


async def encode_pcm_to_opus(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    bitrate: int = 24,
) -> bytes:
    """
    Encode raw PCM audio to Opus format using FFmpeg with stdin/stdout pipes.

    Args:
        pcm_data: Raw PCM audio bytes (signed 16-bit little-endian)
        sample_rate: Sample rate in Hz (default: 16000)
        channels: Number of audio channels (default: 1 for mono)
        bitrate: Opus bitrate in kbps (default: 24 for speech)

    Returns:
        Opus-encoded audio bytes

    Raises:
        RuntimeError: If FFmpeg encoding fails
    """
    # FFmpeg: read PCM from stdin, write Opus to stdout
    # -f ogg wraps opus in an ogg container for stdout piping
    cmd = [
        "ffmpeg",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        f"{bitrate}k",
        "-vbr",
        "on",
        "-application",
        "voip",
        "-f",
        "ogg",
        "pipe:1",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=pcm_data)

    if process.returncode != 0:
        error_msg = stderr.decode() if stderr else "Unknown error"
        logger.error(f"FFmpeg Opus encoding failed: {error_msg}")
        raise RuntimeError(f"Opus encoding failed: {error_msg}")

    logger.debug(
        f"Encoded PCM ({len(pcm_data)} bytes) → Opus ({len(stdout)} bytes), "
        f"compression ratio: {len(stdout)/len(pcm_data):.3f}"
    )

    return stdout


async def decode_opus_to_pcm(
    opus_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    """
    Decode Opus audio to raw PCM format using FFmpeg with stdin/stdout pipes.

    Args:
        opus_data: Opus-encoded audio bytes (ogg/opus container)
        sample_rate: Target sample rate in Hz (default: 16000)
        channels: Target number of channels (default: 1 for mono)

    Returns:
        Raw PCM audio bytes (signed 16-bit little-endian)

    Raises:
        RuntimeError: If FFmpeg decoding fails
    """
    # FFmpeg: read Opus from stdin, write PCM to stdout
    cmd = [
        "ffmpeg",
        "-i",
        "pipe:0",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "pipe:1",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=opus_data)

    if process.returncode != 0:
        error_msg = stderr.decode() if stderr else "Unknown error"
        logger.error(f"FFmpeg Opus decoding failed: {error_msg}")
        raise RuntimeError(f"Opus decoding failed: {error_msg}")

    logger.debug(f"Decoded Opus ({len(opus_data)} bytes) → PCM ({len(stdout)} bytes)")

    return stdout


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
    # Build query. Exclude soft-deleted chunks: a soft-delete (conversation
    # delete, or the de-duplication repair for overlapping reconnect cycles)
    # must not resurface in reconstructed audio.
    query = AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conversation_id,
        AudioChunkDocument.chunk_index >= start_index,
        AudioChunkDocument.deleted == False,  # noqa: E712 (Beanie needs ==)
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


def audio_cache_duration_matches(cached_seconds: float, current_seconds: float) -> bool:
    """Whether a cache built for ``cached_seconds`` still matches the conversation's
    current audio duration (within a small tolerance for partial final chunks).

    Caches derived from the audio-chunk set (waveform, vad_analysis) become stale
    when the chunk set changes in place (e.g. the reconnect-duplicate dedup). The
    serving paths use this to validate the cache against the source of truth
    (``Conversation.audio_total_duration``) and regenerate/ignore it when stale.
    """
    tolerance = max(2.0, 0.02 * max(cached_seconds, current_seconds))
    return abs((cached_seconds or 0.0) - (current_seconds or 0.0)) <= tolerance


async def invalidate_conversation_audio_caches(conversation_id: str) -> dict:
    """Drop caches derived from a conversation's audio chunks (waveform + the
    vad_analysis summary) so they regenerate from the current chunk set.

    Call this whenever a conversation's chunk set changes IN PLACE (same
    conversation_id) — e.g. a re-chunk/trim or a manual dedup repair. Split/merge
    move chunks to fresh conversation_ids, so children regenerate naturally and
    don't need this. Lazy regeneration happens on next read.
    """
    waveforms_deleted = (
        await WaveformData.find(
            WaveformData.conversation_id == conversation_id
        ).delete()
    ).deleted_count

    vad_cleared = False
    conv = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if conv is not None and conv.vad_analysis is not None:
        conv.vad_analysis = None
        await conv.save()
        vad_cleared = True

    return {"waveforms_deleted": waveforms_deleted, "vad_cleared": vad_cleared}


async def get_resume_position(conversation_id: str) -> tuple[int, float]:
    """Next (chunk_index, start_time) for appending audio to a conversation.

    Returns (0, 0.0) when the conversation has no audio yet. When it already has
    chunks — e.g. a WebSocket reconnect re-attaches a persistence cycle to an
    ``always_persist`` placeholder a previous cycle already wrote to — resume from
    the end so new chunks APPEND with a continuous chunk_index/timeline instead of
    restarting at 0. Restarting at 0 produced overlapping duplicate chunks under
    one conversation_id, which corrupts reconstruction (chunks are read sorted by
    chunk_index, so duplicates interleave) and silently truncates playback. The
    ``conversation.audio_chunks_count`` ``max()`` guard only masked the count; this
    fixes the underlying data.
    """
    last = (
        await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id,
            AudioChunkDocument.deleted == False,  # noqa: E712 (Beanie needs ==)
        )
        .sort("-chunk_index")
        .first_or_none()
    )
    if last is None:
        return 0, 0.0
    return last.chunk_index + 1, last.end_time


async def concatenate_chunks_to_pcm(
    chunks: List[AudioChunkDocument],
) -> bytes:
    """
    Decode and concatenate multiple audio chunks into a single PCM buffer.

    Concatenates ogg/opus data from all chunks and decodes in a single ffmpeg
    call, avoiding per-chunk subprocess overhead.

    Args:
        chunks: List of AudioChunkDocument instances (should be pre-sorted)

    Returns:
        Concatenated PCM audio bytes
    """
    if not chunks:
        return b""

    if len(chunks) == 1:
        return await decode_opus_to_pcm(
            opus_data=chunks[0].audio_data,
            sample_rate=chunks[0].sample_rate,
            channels=chunks[0].channels,
        )

    # Concatenate ogg/opus containers into a chained ogg stream.
    # ffmpeg handles chained ogg streams natively — one decode call for all chunks.
    combined_opus = b"".join(bytes(chunk.audio_data) for chunk in chunks)

    pcm_data = await decode_opus_to_pcm(
        opus_data=combined_opus,
        sample_rate=chunks[0].sample_rate,
        channels=chunks[0].channels,
    )

    logger.debug(f"Batch decoded {len(chunks)} chunks → {len(pcm_data)} bytes PCM")

    return pcm_data


async def get_trimmed_opus_for_time_range(
    conversation_id: str, start_time: float, end_time: float
) -> bytes:
    """
    Get exact-trimmed ogg/opus audio for a time range.

    Decodes the stored chunks overlapping the range, clips the PCM to the
    exact boundaries, and re-encodes as a single ogg/opus stream. Raw chunk
    concatenation is not usable here: it is chunk-aligned (~10s granularity)
    and produces a chained ogg stream, which browsers stop playing after the
    first link.

    Args:
        conversation_id: Conversation ID
        start_time: Start time in seconds
        end_time: End time in seconds

    Returns:
        Ogg/opus bytes covering exactly start_time..end_time

    Raises:
        ValueError: If the conversation or range has no audio
    """
    start_timer = time.time()

    clipped_pcm, sample_rate, channels = await get_clipped_pcm_for_time_range(
        conversation_id, start_time, end_time
    )

    if not clipped_pcm:
        raise ValueError(
            f"No audio chunks found for {conversation_id} "
            f"in range {start_time:.1f}s-{end_time:.1f}s"
        )

    opus_data = await encode_pcm_to_opus(
        pcm_data=clipped_pcm,
        sample_rate=sample_rate,
        channels=channels,
    )

    logger.info(
        f"Trimmed opus for {conversation_id[:8]}...: "
        f"{start_time:.1f}s - {end_time:.1f}s "
        f"({len(opus_data)} bytes, {time.time() - start_timer:.2f}s)"
    )

    return opus_data


async def get_opus_for_conversation(
    conversation_id: str,
    start_index: int = 0,
    limit: Optional[int] = None,
) -> bytes:
    """
    Get raw ogg/opus audio for a full conversation by concatenating stored chunks.

    No decoding — returns the original compressed data directly.

    Args:
        conversation_id: Conversation ID
        start_index: First chunk index (default: 0)
        limit: Max chunks (default: all)

    Returns:
        Concatenated ogg/opus bytes

    Raises:
        ValueError: If no chunks found
    """
    chunks = await retrieve_audio_chunks(
        conversation_id=conversation_id,
        start_index=start_index,
        limit=limit,
    )

    if not chunks:
        raise ValueError(f"No audio chunks found for conversation {conversation_id}")

    opus_data = b"".join(bytes(chunk.audio_data) for chunk in chunks)

    logger.info(
        f"Serving {len(chunks)} raw opus chunks for {conversation_id[:8]}... "
        f"({len(opus_data)} bytes)"
    )

    return opus_data


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


async def get_clipped_pcm_for_time_range(
    conversation_id: str, start_time: float, end_time: float
) -> tuple[bytes, int, int]:
    """
    Decode conversation audio and clip it to an exact time range.

    Fetches the MongoDB chunks overlapping the range, batch-decodes them in
    a single ffmpeg call, and clips the PCM to the exact requested
    boundaries (chunk boundaries are ~10s, so decoding alone is not enough).

    Args:
        conversation_id: Conversation ID
        start_time: Start time in seconds
        end_time: End time in seconds

    Returns:
        Tuple of (pcm_data, sample_rate, channels). pcm_data is empty when
        no chunks overlap the range.

    Raises:
        ValueError: If conversation not found, has no audio, or range is invalid
    """
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
        return b"", sample_rate, channels

    # Batch decode all chunks in a single ffmpeg call (concatenated ogg stream)
    pcm_data = await concatenate_chunks_to_pcm(chunks)

    # Clip decoded PCM to the exact requested time range.
    # The full PCM covers chunks[0].start_time .. chunks[-1].end_time.
    bytes_per_second = sample_rate * channels * 2  # 16-bit = 2 bytes per sample
    chunk_range_start = chunks[0].start_time

    clip_start_byte = int((start_time - chunk_range_start) * bytes_per_second)
    clip_start_byte = max(0, (clip_start_byte // 2) * 2)  # align to sample boundary

    clip_end_byte = int((end_time - chunk_range_start) * bytes_per_second)
    clip_end_byte = min(len(pcm_data), (clip_end_byte // 2) * 2)

    return pcm_data[clip_start_byte:clip_end_byte], sample_rate, channels


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
    wav_bytes = (
        await reconstruct_audio_ranges(
            conversation_id,
            [(start_time, end_time)],
        )
    )[0]

    processing_time = time.time() - start_timer

    logger.info(
        f"Reconstructed audio segment for {conversation_id[:8]}...: "
        f"{start_time:.1f}s - {end_time:.1f}s "
        f"({len(wav_bytes)} bytes WAV, "
        f"processing time: {processing_time:.2f}s)"
    )

    return wav_bytes


async def reconstruct_audio_ranges(
    conversation_id: str,
    ranges: List[tuple[float, float]],
    *,
    max_window_seconds: float = 120.0,
) -> List[bytes]:
    """Reconstruct ordered ranges while decoding each bounded audio window once.

    Unlike :func:`reconstruct_audio_segment`, this corpus-oriented path never
    materializes the multi-megabyte Conversation model. It projects only duration,
    groups nearby transcript ranges into bounded windows, fetches raw chunk documents,
    and slices several WAV clips from each decoded PCM buffer.
    """
    if not ranges:
        return []
    if max_window_seconds <= 0:
        raise ValueError("max_window_seconds must be positive")

    conversation = await Conversation.get_pymongo_collection().find_one(
        {"conversation_id": conversation_id, "deleted": {"$ne": True}},
        {"_id": 0, "audio_total_duration": 1},
    )
    if conversation is None:
        raise ValueError(f"Conversation {conversation_id} not found")
    total_duration = float(conversation.get("audio_total_duration") or 0.0)
    if total_duration <= 0:
        raise ValueError(f"Conversation {conversation_id} has no audio")

    indexed_ranges = []
    for index, (start_time, end_time) in enumerate(ranges):
        start = float(start_time)
        end = min(float(end_time), total_duration)
        if start < 0 or end <= start:
            raise ValueError(
                f"Invalid time range [{start_time}, {end_time}] for {conversation_id}"
            )
        indexed_ranges.append((index, start, end))
    indexed_ranges.sort(key=lambda item: (item[1], item[2]))

    windows: List[List[tuple[int, float, float]]] = []
    for item in indexed_ranges:
        if not windows or item[2] - windows[-1][0][1] > max_window_seconds:
            windows.append([item])
        else:
            windows[-1].append(item)

    results: List[Optional[bytes]] = [None] * len(ranges)
    chunk_collection = AudioChunkDocument.get_pymongo_collection()
    for window in windows:
        window_start = min(item[1] for item in window)
        window_end = max(item[2] for item in window)
        chunks = await (
            chunk_collection.find(
                {
                    "conversation_id": conversation_id,
                    "start_time": {"$lt": window_end},
                    "end_time": {"$gt": window_start},
                    "deleted": {"$ne": True},
                },
                {
                    "_id": 0,
                    "audio_data": 1,
                    "start_time": 1,
                    "end_time": 1,
                    "chunk_index": 1,
                    "sample_rate": 1,
                    "channels": 1,
                },
            )
            .sort("chunk_index", 1)
            .to_list(length=None)
        )
        if not chunks:
            raise ValueError(
                f"No audio chunks cover [{window_start}, {window_end}] in {conversation_id}"
            )
        chunk_islands: List[List[dict]] = []
        for chunk in chunks:
            if (
                not chunk_islands
                or float(chunk["start_time"]) - float(chunk_islands[-1][-1]["end_time"])
                > 0.25
            ):
                chunk_islands.append([chunk])
            else:
                chunk_islands[-1].append(chunk)

        for island in chunk_islands:
            island_start = float(island[0]["start_time"])
            island_end = float(island[-1]["end_time"])
            island_ranges = [
                item
                for item in window
                if item[1] >= island_start - 0.25 and item[2] <= island_end + 0.25
            ]
            if not island_ranges:
                continue

            sample_rate = int(island[0]["sample_rate"])
            channels = int(island[0]["channels"])
            if any(
                int(chunk["sample_rate"]) != sample_rate
                or int(chunk["channels"]) != channels
                for chunk in island
            ):
                raise ValueError(
                    "Audio format changes inside one reconstruction island"
                )
            pcm_data = await decode_opus_to_pcm(
                opus_data=b"".join(bytes(chunk["audio_data"]) for chunk in island),
                sample_rate=sample_rate,
                channels=channels,
            )
            bytes_per_frame = channels * 2
            bytes_per_second = sample_rate * bytes_per_frame
            tolerance_bytes = int(0.25 * bytes_per_second)

            for original_index, start, end in island_ranges:
                start_byte = int((start - island_start) * bytes_per_second)
                end_byte = int((end - island_start) * bytes_per_second)
                start_byte = max(0, start_byte - start_byte % bytes_per_frame)
                end_byte = min(len(pcm_data), end_byte - end_byte % bytes_per_frame)
                expected_bytes = int((end - start) * bytes_per_second)
                clipped = pcm_data[start_byte:end_byte]
                if len(clipped) + tolerance_bytes < expected_bytes:
                    raise ValueError(
                        f"Decoded audio is too short for range [{start}, {end}] "
                        f"in {conversation_id}"
                    )
                results[original_index] = await build_wav_from_pcm(
                    clipped,
                    sample_rate=sample_rate,
                    channels=channels,
                )

        missing = [item for item in window if results[item[0]] is None]
        if missing:
            _, start, end = missing[0]
            raise ValueError(
                f"Audio chunk gap prevents exact range reconstruction for "
                f"[{start}, {end}] in {conversation_id}"
            )

    if any(result is None for result in results):
        raise RuntimeError("Range reconstruction did not produce every requested clip")
    return [result for result in results if result is not None]


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
    captured_at: Optional[datetime] = None,
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
        captured_at: Absolute UTC time the first sample was captured. Each chunk
            records its own ``captured_at`` from this, which is what survives the
            renumbering done by split, merge and silence trimming.

    Returns:
        Number of chunks created

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
    logger.info(f"📦 Converting audio to MongoDB chunks: {len(audio_data)} bytes PCM")

    # Calculate audio duration
    bytes_per_second = sample_rate * sample_width * channels
    total_duration_seconds = len(audio_data) / bytes_per_second

    # Calculate chunk size in bytes
    chunk_size_bytes = int(chunk_duration * bytes_per_second)

    # Insert in batches of 100 chunks (~16 min at 10s/chunk) to avoid
    # accumulating all chunks in memory for very long audio files.
    BATCH_INSERT_SIZE = 100
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
            captured_at=(
                captured_at + timedelta(seconds=chunk_start_time)
                if captured_at is not None
                else None
            ),
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

        # Flush batch to MongoDB when batch size reached
        if len(chunks_to_insert) >= BATCH_INSERT_SIZE:
            await AudioChunkDocument.insert_many(chunks_to_insert)
            logger.info(
                f"✅ Batch inserted {len(chunks_to_insert)} chunks to MongoDB "
                f"(chunks {chunk_index - len(chunks_to_insert)}-{chunk_index - 1})"
            )
            chunks_to_insert = []

    # Insert remaining chunks
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
    captured_at: Optional[datetime] = None,
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

    logger.info(f"📦 Converting WAV file to MongoDB chunks: {wav_file_path}")

    # Read WAV file
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

    # Calculate audio duration
    bytes_per_second = sample_rate * sample_width * channels
    total_duration_seconds = len(pcm_data) / bytes_per_second

    # Calculate chunk size in bytes
    chunk_size_bytes = int(chunk_duration * bytes_per_second)

    # Insert in batches of 100 chunks (~16 min at 10s/chunk)
    BATCH_INSERT_SIZE = 100
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
            captured_at=(
                captured_at + timedelta(seconds=chunk_start_time)
                if captured_at is not None
                else None
            ),
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

        # Flush batch to MongoDB when batch size reached
        if len(chunks_to_insert) >= BATCH_INSERT_SIZE:
            await AudioChunkDocument.insert_many(chunks_to_insert)
            logger.info(
                f"✅ Batch inserted {len(chunks_to_insert)} chunks to MongoDB "
                f"(chunks {chunk_index - len(chunks_to_insert)}-{chunk_index - 1})"
            )
            chunks_to_insert = []

    # Insert remaining chunks
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

    # Log at WARNING (not ERROR): this generic util doesn't know *why* the session
    # ended. A benign client disconnect (device walked out of range — no audio ever
    # persisted) and a genuine persistence failure both land here. The caller knows
    # the end reason and emits the severity-appropriate line, so emitting ERROR here
    # would raise a spurious system event for every dead-zone reconnect.
    logger.warning(
        f"⏱️ Audio chunks not found after {max_wait_seconds}s "
        f"(conversation: {conversation_id[:12]}) — caller decides how to handle"
    )
    return False


# Peak target for a normalized preview. Not full scale: a little headroom keeps a
# boosted clip from clipping on playback.
PREVIEW_PEAK = 0.89
# Beyond this the source is silence, and multiplying silence only produces loud
# silence while making the noise floor sound like a fault.
MAX_PREVIEW_GAIN = 200.0


def normalize_wav_peak(wav: bytes) -> tuple[bytes, float]:
    """Peak-normalize a WAV clip, returning it with the applied gain in dB.

    Written for review tools that ask a human to judge audio. The clips worth judging
    are often far below the ones that behave normally — measured across one episode,
    windows where VAD reported speech but the transcriber produced nothing ran -43 to
    -58 dBFS RMS, against -15 to -42 for windows that transcribed cleanly. Played raw
    those are indistinguishable from silence, and a listener concludes the tool is
    broken rather than that the audio is quiet.

    The gain is returned rather than swallowed, because it is itself evidence: "this
    needed +42 dB" says something about the window, and anyone judging loudness has to
    know they are not hearing the original level.

    Non-16-bit input is returned untouched with zero gain.
    """

    with wave.open(io.BytesIO(wav)) as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    if params.sampwidth != 2 or not frames:
        return wav, 0.0
    samples = array.array("h")
    samples.frombytes(frames)
    peak = max((abs(value) for value in samples), default=0)
    if peak == 0:
        return wav, 0.0
    gain = min(PREVIEW_PEAK * 32767 / peak, MAX_PREVIEW_GAIN)
    if gain <= 1.0:
        return wav, 0.0
    boosted = array.array(
        "h", (max(-32768, min(32767, int(value * gain))) for value in samples)
    )
    out = io.BytesIO()
    with wave.open(out, "wb") as sink:
        sink.setparams(params)
        sink.writeframes(boosted.tobytes())
    return out.getvalue(), round(20 * math.log10(gain), 1)
