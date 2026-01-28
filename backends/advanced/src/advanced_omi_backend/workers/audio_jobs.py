"""
Audio-related RQ job functions.

This module contains jobs related to audio file processing and cropping.
"""

import asyncio
import os
import logging
import time
from typing import Dict, Any, Optional

from advanced_omi_backend.models.job import JobPriority, async_job

from advanced_omi_backend.controllers.queue_controller import (
    default_queue,
    JOB_RESULT_TTL,
)
from advanced_omi_backend.models.job import _ensure_beanie_initialized

logger = logging.getLogger(__name__)


@async_job(redis=True, beanie=True)
async def audio_streaming_persistence_job(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    redis_client=None
) -> Dict[str, Any]:
    """
    Long-running RQ job that stores audio chunks in MongoDB with Opus compression.

    Buffers incoming PCM audio from Redis Stream into 10-second chunks, encodes
    them to Opus format, and stores in MongoDB audio_chunks collection.

    Runs in parallel with transcription processing to reduce memory pressure.

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with chunk_count, total_bytes, compressed_bytes, duration_seconds

    Note:
        - Replaces disk-based WAV file storage with MongoDB chunk storage.
        - Reads always_persist_enabled from global config to determine whether to
          create placeholder conversations immediately.
    """
    # Read always_persist setting from global config
    from advanced_omi_backend.config import get_misc_settings
    misc_settings = get_misc_settings()
    always_persist = misc_settings.get('always_persist_enabled', False)

    logger.info(f"🎵 Starting MongoDB audio persistence for session {session_id} (always_persist={always_persist})")

    # Setup audio persistence consumer group (separate from transcription consumer)
    audio_stream_name = f"audio:stream:{client_id}"
    audio_group_name = "audio_persistence"
    audio_consumer_name = f"persistence-{session_id[:8]}"

    try:
        await redis_client.xgroup_create(
            audio_stream_name,
            audio_group_name,
            "0",
            mkstream=True
        )
        logger.info(f"📦 Created audio persistence consumer group for {audio_stream_name}")
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.warning(f"Failed to create audio consumer group: {e}")
        logger.debug(f"Audio consumer group already exists for {audio_stream_name}")

    # If always_persist enabled, create placeholder conversation if it doesn't exist
    if always_persist:
        conversation_key = f"conversation:current:{session_id}"
        existing_conversation_id = await redis_client.get(conversation_key)

        if not existing_conversation_id:
            logger.info(
                f"📝 always_persist=True - creating placeholder conversation for session {session_id[:12]}"
            )

            # Import conversation model
            from advanced_omi_backend.models.conversation import Conversation

            # Create placeholder conversation
            conversation = Conversation(
                user_id=user_id,
                client_id=client_id,
                title="Audio Recording (Processing...)",
                summary="Transcription in progress...",
                transcript_versions=[],
                memory_versions=[],
                processing_status="pending_transcription",
                always_persist=True
            )
            await conversation.insert()

            # Set conversation:current Redis key
            await redis_client.set(
                conversation_key,
                conversation.conversation_id,
                ex=3600  # 1 hour expiry
            )

            logger.info(
                f"✅ Created placeholder conversation {conversation.conversation_id} "
                f"and set Redis key {conversation_key}"
            )
        else:
            logger.info(
                f"📋 always_persist=True - placeholder conversation already exists: "
                f"{existing_conversation_id.decode()}"
            )
    else:
        logger.info(
            f"🔍 always_persist=False - will wait for speech detection to create conversation"
        )

    # Job control
    session_key = f"audio:session:{session_id}"
    max_runtime = 86340  # 24 hours - 60 seconds (graceful exit before RQ timeout)
    start_time = time.time()

    # Import MongoDB chunk utilities
    from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.utils.audio_chunk_utils import encode_pcm_to_opus
    from bson import Binary

    # Conversation rotation state
    current_conversation_id = None
    conversation_start_time = None
    conversation_count = 0

    # PCM buffer for current 10-second chunk
    pcm_buffer = bytearray()
    chunk_index = 0  # Sequential chunk counter for current conversation
    chunk_start_time = 0.0  # Start time of current buffered chunk

    # Chunk configuration
    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2  # 16-bit
    CHANNELS = 1  # Mono
    CHUNK_DURATION_SECONDS = 10.0
    BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS  # 32,000 bytes/sec
    CHUNK_SIZE_BYTES = int(CHUNK_DURATION_SECONDS * BYTES_PER_SECOND)  # 320,000 bytes

    # Session stats (across all conversations)
    total_pcm_bytes = 0
    total_compressed_bytes = 0
    total_mongo_chunks_written = 0
    end_signal_received = False
    consecutive_empty_reads = 0
    max_empty_reads = 3

    # Get current job for zombie detection
    from rq import get_current_job
    from advanced_omi_backend.utils.job_utils import check_job_alive
    current_job = get_current_job()

    async def flush_pcm_buffer():
        """
        Flush current PCM buffer to MongoDB as Opus-compressed chunk.

        Updates conversation metadata with chunk count and compression stats.
        """
        nonlocal pcm_buffer, chunk_index, chunk_start_time
        nonlocal total_pcm_bytes, total_compressed_bytes, total_mongo_chunks_written

        if len(pcm_buffer) == 0 or not current_conversation_id:
            return

        try:
            # Encode PCM → Opus
            opus_data = await encode_pcm_to_opus(
                pcm_data=bytes(pcm_buffer),
                sample_rate=SAMPLE_RATE,
                channels=CHANNELS,
                bitrate=24  # 24kbps for speech
            )

            # Calculate chunk metadata
            original_size = len(pcm_buffer)
            compressed_size = len(opus_data)
            duration = original_size / BYTES_PER_SECOND
            end_time = chunk_start_time + duration

            # Create MongoDB document
            audio_chunk = AudioChunkDocument(
                conversation_id=current_conversation_id,
                chunk_index=chunk_index,
                audio_data=Binary(opus_data),
                original_size=original_size,
                compressed_size=compressed_size,
                start_time=chunk_start_time,
                end_time=end_time,
                duration=duration,
                sample_rate=SAMPLE_RATE,
                channels=CHANNELS,
            )

            # Save to MongoDB
            await audio_chunk.insert()

            # Update session stats
            total_pcm_bytes += original_size
            total_compressed_bytes += compressed_size
            total_mongo_chunks_written += 1

            # Update conversation metadata
            conversation = await Conversation.find_one(
                Conversation.conversation_id == current_conversation_id
            )

            if conversation:
                # Calculate running totals
                chunk_count = chunk_index + 1
                total_duration = end_time
                compression_ratio = compressed_size / original_size if original_size > 0 else 0.0

                # Update conversation fields
                conversation.audio_chunks_count = chunk_count
                conversation.audio_total_duration = total_duration
                conversation.audio_compression_ratio = compression_ratio
                await conversation.save()

            logger.debug(
                f"💾 Saved chunk {chunk_index} for conversation {current_conversation_id[:12]}: "
                f"{original_size} → {compressed_size} bytes ({compression_ratio:.3f} ratio), "
                f"{duration:.1f}s duration"
            )

            # Log every 6 chunks (60 seconds) to avoid spam
            if (chunk_index + 1) % 6 == 0:
                logger.info(
                    f"📦 Conversation {current_conversation_id[:12]}: "
                    f"{chunk_index + 1} chunks, {total_duration:.1f}s total"
                )

        except Exception as e:
            logger.error(f"❌ Failed to save audio chunk {chunk_index}: {e}", exc_info=True)

    while True:
        # Check if job still exists in Redis (detect zombie state)
        if not await check_job_alive(redis_client, current_job, session_id):
            # Flush remaining buffer before exit
            if len(pcm_buffer) > 0:
                await flush_pcm_buffer()
            break

        # Check timeout
        if time.time() - start_time > max_runtime:
            logger.warning(f"⏱️ Timeout reached for audio persistence {session_id}")
            # Flush remaining buffer
            if len(pcm_buffer) > 0:
                await flush_pcm_buffer()
            break

        # Check if session is finalizing
        session_status = await redis_client.hget(session_key, "status")
        if session_status and session_status.decode() in ["finalizing", "finished"]:
            logger.info(f"🛑 Session finalizing detected, flushing final chunks...")
            await asyncio.sleep(0.5)  # Brief wait for in-flight chunks

            # Final read to collect remaining chunks
            try:
                final_messages = await redis_client.xreadgroup(
                    audio_group_name,
                    audio_consumer_name,
                    {audio_stream_name: ">"},
                    count=50,
                    block=500
                )

                if final_messages:
                    for stream_name, msgs in final_messages:
                        for message_id, fields in msgs:
                            audio_data = fields.get(b"audio_data", b"")
                            chunk_id = fields.get(b"chunk_id", b"").decode()

                            if chunk_id != "END" and len(audio_data) > 0:
                                pcm_buffer.extend(audio_data)

                                # Flush if buffer reaches chunk size
                                if len(pcm_buffer) >= CHUNK_SIZE_BYTES:
                                    await flush_pcm_buffer()
                                    # Reset for next chunk
                                    pcm_buffer = bytearray()
                                    chunk_index += 1
                                    chunk_start_time += CHUNK_DURATION_SECONDS

                            await redis_client.xack(audio_stream_name, audio_group_name, message_id)

                    logger.info(f"📦 Final read processed {len(final_messages[0][1])} messages")

            except Exception as e:
                logger.debug(f"Final audio read error (non-fatal): {e}")

            # Flush any remaining partial chunk
            if len(pcm_buffer) > 0:
                await flush_pcm_buffer()

            break

        # Check for conversation change (rotation signal)
        conversation_key = f"conversation:current:{session_id}"
        new_conversation_id = await redis_client.get(conversation_key)

        if new_conversation_id:
            new_conversation_id = new_conversation_id.decode()

            # Conversation changed - flush current buffer and rotate
            if new_conversation_id != current_conversation_id:
                # Flush remaining buffer from previous conversation
                if len(pcm_buffer) > 0 and current_conversation_id:
                    await flush_pcm_buffer()
                    logger.info(
                        f"✅ Finalized conversation {current_conversation_id[:12]}: "
                        f"{chunk_index + 1} chunks saved to MongoDB"
                    )

                # Start new conversation
                current_conversation_id = new_conversation_id
                conversation_count += 1
                conversation_start_time = time.time()

                # Reset chunk state
                pcm_buffer = bytearray()
                chunk_index = 0
                chunk_start_time = 0.0

                logger.info(
                    f"📁 Started MongoDB persistence for conversation #{conversation_count} "
                    f"({current_conversation_id[:12]})"
                )
        else:
            # Conversation key deleted - conversation ended
            if current_conversation_id and len(pcm_buffer) > 0:
                # Flush final partial chunk
                await flush_pcm_buffer()
                duration = (time.time() - conversation_start_time) if conversation_start_time else 0
                logger.info(
                    f"✅ Conversation {current_conversation_id[:12]} ended: "
                    f"{chunk_index + 1} chunks, {duration:.1f}s"
                )

                # Reset state
                pcm_buffer = bytearray()
                current_conversation_id = None

        # Wait for conversation to be created
        if not current_conversation_id:
            await asyncio.sleep(0.0001)
            continue

        # Read audio chunks from Redis Stream
        try:
            audio_messages = await redis_client.xreadgroup(
                audio_group_name,
                audio_consumer_name,
                {audio_stream_name: ">"},
                count=20,  # Read up to 20 chunks at a time
                block=100  # 100ms timeout
            )

            if audio_messages:
                consecutive_empty_reads = 0  # Reset counter

                for stream_name, msgs in audio_messages:
                    for message_id, fields in msgs:
                        audio_data = fields.get(b"audio_data", b"")
                        chunk_id = fields.get(b"chunk_id", b"").decode()

                        # Check for END signal
                        if chunk_id == "END":
                            logger.info(f"📡 Received END signal in audio persistence")
                            end_signal_received = True
                        elif len(audio_data) > 0:
                            # Append to PCM buffer
                            pcm_buffer.extend(audio_data)

                            # Flush if buffer reaches 10-second chunk size
                            if len(pcm_buffer) >= CHUNK_SIZE_BYTES:
                                await flush_pcm_buffer()

                                # Reset for next chunk
                                pcm_buffer = bytearray()
                                chunk_index += 1
                                chunk_start_time += CHUNK_DURATION_SECONDS

                        # ACK the message
                        await redis_client.xack(audio_stream_name, audio_group_name, message_id)

            else:
                # No new messages
                if end_signal_received:
                    consecutive_empty_reads += 1
                    logger.info(f"📭 No new messages ({consecutive_empty_reads}/{max_empty_reads})")

                    if consecutive_empty_reads >= max_empty_reads:
                        logger.info(f"✅ Stream empty after END signal - stopping")
                        # Flush remaining buffer
                        if len(pcm_buffer) > 0:
                            await flush_pcm_buffer()
                        break

        except Exception as audio_error:
            logger.debug(f"Audio stream read error (non-fatal): {audio_error}")

        await asyncio.sleep(0.0001)

    # Job complete - calculate final stats
    runtime_seconds = time.time() - start_time

    # Calculate total duration
    if total_pcm_bytes > 0:
        duration = total_pcm_bytes / BYTES_PER_SECOND
        compression_ratio = total_compressed_bytes / total_pcm_bytes if total_pcm_bytes > 0 else 0.0
    else:
        logger.warning(f"⚠️ No audio chunks written for session {session_id}")
        duration = 0.0
        compression_ratio = 0.0

    logger.info(
        f"🎵 MongoDB audio persistence complete for session {session_id}: "
        f"{conversation_count} conversations, {total_mongo_chunks_written} chunks, "
        f"{total_pcm_bytes / 1024 / 1024:.2f} MB PCM → {total_compressed_bytes / 1024 / 1024:.2f} MB Opus "
        f"(compression: {compression_ratio:.3f}, {(1 - compression_ratio) * 100:.1f}% savings), "
        f"{runtime_seconds:.1f}s runtime"
    )

    # Clean up Redis tracking keys
    audio_job_key = f"audio_persistence:session:{session_id}"
    await redis_client.delete(audio_job_key)

    # NOTE: Do NOT delete conversation:current:{session_id} key here!
    # It's needed for speech detection to reuse placeholder conversations (always_persist feature).
    # The key already has a TTL (3600s) set when created and will expire automatically.
    logger.info(f"🧹 Cleaned up tracking keys for session {session_id}")

    return {
        "session_id": session_id,
        "conversation_count": conversation_count,
        "total_mongo_chunks": total_mongo_chunks_written,
        "total_pcm_bytes": total_pcm_bytes,
        "total_compressed_bytes": total_compressed_bytes,
        "compression_ratio": compression_ratio,
        "duration_seconds": duration,
        "runtime_seconds": runtime_seconds
    }


# Enqueue wrapper functions
