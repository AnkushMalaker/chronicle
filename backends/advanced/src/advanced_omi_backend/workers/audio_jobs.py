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
async def process_cropping_job(
    conversation_id: str,
    audio_path: str,
    *,
    redis_client=None
) -> Dict[str, Any]:
    """
    RQ job function for audio cropping - removes silent segments from audio.

    This job:
    1. Reads transcript segments from conversation
    2. Extracts speech timestamps
    3. Creates cropped audio file with only speech segments
    4. Updates conversation with cropped file path

    Args:
        conversation_id: Conversation ID
        audio_path: Path to original audio file
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with processing results
    """
    from pathlib import Path
    from advanced_omi_backend.utils.audio_utils import _process_audio_cropping_with_relative_timestamps
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.config import CHUNK_DIR

    try:
        logger.info(f"🔄 RQ: Starting audio cropping for conversation {conversation_id}")

        # Get conversation to access segments
        conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
        if not conversation:
            raise ValueError(f"Conversation {conversation_id} not found")

        # Extract speech segments from transcript (property returns data from active version)
        segments = conversation.segments
        if not segments or len(segments) == 0:
            logger.warning(f"⚠️ No segments found for conversation {conversation_id}, skipping cropping")
            return {
                "success": False,
                "conversation_id": conversation_id,
                "reason": "no_segments"
            }

        # Convert segments to (start, end) tuples
        speech_segments = [(seg.start, seg.end) for seg in segments]
        logger.info(f"Found {len(speech_segments)} speech segments for cropping")

        # Generate output path for cropped audio
        audio_uuid = conversation.audio_uuid

        # Build full path from conversation.audio_path (which may include folder prefix)
        # conversation.audio_path is like "fixtures/filename.wav" or just "filename.wav"
        full_audio_path = CHUNK_DIR / conversation.audio_path
        original_path = Path(full_audio_path)
        cropped_filename = f"cropped_{original_path.name}"

        # If the conversation's audio_path contains a folder prefix, use the same folder for cropped audio
        if conversation.audio_path and "/" in conversation.audio_path:
            folder = conversation.audio_path.split("/")[0]
            output_dir = CHUNK_DIR / folder
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / cropped_filename
            cropped_path_for_db = f"{folder}/{cropped_filename}"
        else:
            output_path = CHUNK_DIR / cropped_filename
            cropped_path_for_db = cropped_filename

        # Process cropping (no repository needed - we update conversation directly)
        success, segment_mapping = await _process_audio_cropping_with_relative_timestamps(
            str(original_path),
            speech_segments,
            str(output_path),
            audio_uuid,
            None  # No repository - we update conversation model directly
        )

        if not success:
            logger.error(f"❌ RQ: Audio cropping failed for conversation {conversation_id}")
            return {
                "success": False,
                "conversation_id": conversation_id,
                "reason": "cropping_failed"
            }

        # Calculate actual cropped duration from kept segments
        kept_segments = [m for m in segment_mapping if m["kept"]]
        if kept_segments:
            # Duration is end of last kept segment
            cropped_duration_seconds = kept_segments[-1]["cropped_end"]
        else:
            cropped_duration_seconds = 0.0

        # Update segment timestamps using the mapping
        # Only keep segments that weren't filtered out
        updated_segments = []
        for i, seg in enumerate(segments):
            if i >= len(segment_mapping):
                logger.warning(f"⚠️ Segment {i} not in mapping, skipping")
                continue

            mapping = segment_mapping[i]
            if mapping["kept"]:
                # Segment was kept - use the cropped timestamps
                updated_seg = seg.model_copy()
                updated_seg.start = mapping["cropped_start"]
                updated_seg.end = mapping["cropped_end"]
                updated_segments.append(updated_seg)
                logger.debug(
                    f"Segment {i}: {seg.start:.2f}-{seg.end:.2f}s → "
                    f"{updated_seg.start:.2f}-{updated_seg.end:.2f}s (in cropped audio)"
                )
            else:
                # Segment was filtered out (too short)
                logger.debug(
                    f"Segment {i} filtered out (duration {seg.end - seg.start:.2f}s < MIN_SPEECH_SEGMENT_DURATION)"
                )

        # Update conversation with cropped audio path and adjusted segments
        conversation.cropped_audio_path = cropped_path_for_db

        # Update the active transcript version segments
        # Find and update the version directly in the list to ensure Beanie detects the change
        if conversation.active_transcript_version:
            for i, version in enumerate(conversation.transcript_versions):
                if version.version_id == conversation.active_transcript_version:
                    conversation.transcript_versions[i].segments = updated_segments
                    logger.info(f"📝 Updated segments in transcript version {version.version_id[:12]}")
                    break

        await conversation.save()
        logger.info(f"💾 Updated conversation {conversation_id[:12]} with cropped_audio_path and adjusted {len(updated_segments)} segment timestamps")

        logger.info(f"✅ RQ: Completed audio cropping for conversation {conversation_id} ({cropped_duration_seconds:.1f}s)")

        # Update job metadata with cropped duration
        from rq import get_current_job
        current_job = get_current_job()
        if current_job:
            if not current_job.meta:
                current_job.meta = {}
            current_job.meta['cropped_duration_seconds'] = round(cropped_duration_seconds, 1)
            current_job.meta['segments_cropped'] = len(speech_segments)
            current_job.save_meta()

        return {
            "success": True,
            "conversation_id": conversation_id,
            "audio_uuid": audio_uuid,
            "original_path": str(original_path),
            "cropped_path": str(output_path),
            "cropped_filename": cropped_filename,
            "segments_count": len(speech_segments),
            "cropped_duration_seconds": cropped_duration_seconds
        }

    except Exception as e:
        logger.exception(f"❌ RQ: Audio cropping failed for conversation {conversation_id}: {e}")
        raise


@async_job(redis=True, beanie=True)
async def audio_streaming_persistence_job(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    redis_client=None
) -> Dict[str, Any]:
    """
    Long-running RQ job that progressively writes audio chunks to disk as they arrive.

    Opens a WAV file immediately and appends chunks in real-time, making the file
    available for playback in the UI before the session completes.

    Runs in parallel with transcription processing to reduce memory pressure.

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with audio_file_path, chunk_count, total_bytes, duration_seconds

    Note: user_email is fetched from the database when needed.
    """
    logger.info(f"🎵 Starting audio persistence for session {session_id}")

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
            logger.exception(f"Failed to create audio consumer group: {e}")
        logger.debug(f"Audio consumer group already exists for {audio_stream_name}")

    # Job control
    session_key = f"audio:session:{session_id}"
    max_runtime = 86340  # 24 hours - 60 seconds (graceful exit before RQ timeout)
    start_time = time.time()

    from advanced_omi_backend.config import CHUNK_DIR
    from easy_audio_interfaces.filesystem.filesystem_interfaces import LocalFileSink
    from wyoming.audio import AudioChunk

    # Ensure directory exists
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)

    # File rotation state
    current_conversation_id = None
    file_sink = None
    file_path = None
    wav_filename = None
    conversation_chunk_count = 0
    conversation_start_time = None

    # Audio collection stats (across all conversations in this session)
    total_chunk_count = 0
    total_bytes = 0
    end_signal_received = False
    consecutive_empty_reads = 0
    max_empty_reads = 3  # Exit after 3 consecutive empty reads (deterministic check)
    conversation_count = 0

    # Get current job for zombie detection
    from rq import get_current_job
    from advanced_omi_backend.utils.job_utils import check_job_alive
    current_job = get_current_job()

    while True:
        # Check if job still exists in Redis (detect zombie state)
        if not await check_job_alive(redis_client, current_job):
            if file_sink:
                await file_sink.close()
            break

        # Check timeout
        if time.time() - start_time > max_runtime:
            logger.warning(f"⏱️ Timeout reached for audio persistence {session_id}")
            # Close current file if open
            if file_sink:
                await file_sink.close()
                logger.info(f"✅ Closed file on timeout: {wav_filename}")
            break

        # Check if session is finalizing (user stopped recording or WebSocket disconnected)
        session_status = await redis_client.hget(session_key, "status")
        if session_status and session_status.decode() in ["finalizing", "complete"]:
            logger.info(f"🛑 Session finalizing detected, writing final chunks...")
            # Give a brief moment for any in-flight chunks to arrive
            await asyncio.sleep(0.5)
            # Do one final read to write remaining chunks to current file
            if file_sink:
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
                                    chunk = AudioChunk(
                                        rate=16000,
                                        width=2,
                                        channels=1,
                                        audio=audio_data
                                    )
                                    await file_sink.write(chunk)
                                    conversation_chunk_count += 1
                                    total_chunk_count += 1
                                    total_bytes += len(audio_data)
                                await redis_client.xack(audio_stream_name, audio_group_name, message_id)
                        logger.info(f"📦 Final read wrote {len(final_messages[0][1]) if final_messages else 0} more chunks")
                except Exception as e:
                    logger.debug(f"Final audio read error (non-fatal): {e}")

                # Close final file
                await file_sink.close()
                logger.info(f"✅ Closed final file: {wav_filename} ({conversation_chunk_count} chunks)")
            break

        # Check for conversation change (file rotation signal)
        conversation_key = f"conversation:current:{session_id}"
        new_conversation_id = await redis_client.get(conversation_key)

        if new_conversation_id:
            new_conversation_id = new_conversation_id.decode()

            # Conversation changed - rotate to new file
            if new_conversation_id != current_conversation_id:
                # Close previous file if exists
                if file_sink:
                    await file_sink.close()
                    duration = (time.time() - conversation_start_time) if conversation_start_time else 0
                    logger.info(
                        f"✅ Closed conversation {current_conversation_id[:12]} file: {wav_filename} "
                        f"({conversation_chunk_count} chunks, {duration:.1f}s)"
                    )

                # Open new file for new conversation
                current_conversation_id = new_conversation_id
                conversation_count += 1
                conversation_chunk_count = 0
                conversation_start_time = time.time()

                timestamp = int(time.time() * 1000)
                wav_filename = f"{timestamp}_{client_id}_{current_conversation_id}.wav"
                file_path = CHUNK_DIR / wav_filename

                file_sink = LocalFileSink(
                    file_path=str(file_path),
                    sample_rate=16000,
                    channels=1,
                    sample_width=2
                )
                await file_sink.open()
                logger.info(
                    f"📁 Opened new file for conversation #{conversation_count} ({current_conversation_id[:12]}): {file_path}"
                )

                # Store file path in Redis (keyed by conversation_id, not session_id)
                audio_file_key = f"audio:file:{current_conversation_id}"
                await redis_client.set(audio_file_key, str(file_path), ex=86400)  # 24 hour TTL
                logger.info(f"💾 Stored audio file path in Redis: {audio_file_key}")
        else:
            # Key deleted - conversation ended, close current file
            if file_sink and current_conversation_id:
                await file_sink.close()
                duration = (time.time() - conversation_start_time) if conversation_start_time else 0
                logger.info(
                    f"✅ Closed conversation {current_conversation_id[:12]} file after conversation ended: {wav_filename} "
                    f"({conversation_chunk_count} chunks, {duration:.1f}s)"
                )
                file_sink = None  # Clear sink to prevent writing to closed file
                current_conversation_id = None

        # If no file open yet, wait for conversation to be created
        if not file_sink:
            await asyncio.sleep(0.5)
            continue

        # Read audio chunks from stream (non-blocking)
        try:
            audio_messages = await redis_client.xreadgroup(
                audio_group_name,
                audio_consumer_name,
                {audio_stream_name: ">"},
                count=20,  # Read up to 20 chunks at a time for efficiency
                block=500  # 500ms timeout
            )

            if audio_messages:
                # Reset empty read counter - we got messages
                consecutive_empty_reads = 0

                for stream_name, msgs in audio_messages:
                    for message_id, fields in msgs:
                        # Extract audio data
                        audio_data = fields.get(b"audio_data", b"")
                        chunk_id = fields.get(b"chunk_id", b"").decode()

                        # Check for END signal
                        if chunk_id == "END":
                            logger.info(f"📡 Received END signal in audio persistence")
                            end_signal_received = True
                        elif len(audio_data) > 0:
                            # Write chunk immediately to file
                            chunk = AudioChunk(
                                rate=16000,
                                width=2,
                                channels=1,
                                audio=audio_data
                            )
                            await file_sink.write(chunk)
                            conversation_chunk_count += 1
                            total_chunk_count += 1
                            total_bytes += len(audio_data)

                            # Log every 40 chunks to avoid spam
                            if total_chunk_count % 40 == 0:
                                logger.info(
                                    f"📦 Session {session_id[:12]}: {total_chunk_count} total chunks "
                                    f"(conversation {current_conversation_id[:12]}: {conversation_chunk_count} chunks)"
                                )

                        # ACK the message
                        await redis_client.xack(audio_stream_name, audio_group_name, message_id)
            else:
                # No new messages - stream might be empty
                if end_signal_received:
                    consecutive_empty_reads += 1
                    logger.info(f"📭 No new messages ({consecutive_empty_reads}/{max_empty_reads} empty reads after END signal)")

                    if consecutive_empty_reads >= max_empty_reads:
                        logger.info(f"✅ Stream empty after END signal - stopping audio collection")
                        break

        except Exception as audio_error:
            # Stream might not exist yet or other transient errors
            logger.debug(f"Audio stream read error (non-fatal): {audio_error}")

        await asyncio.sleep(0.1)  # Check every 100ms for responsiveness

    # Job complete - calculate final stats
    runtime_seconds = time.time() - start_time

    # Calculate duration (16kHz, 16-bit mono = 32000 bytes/second)
    if total_bytes > 0:
        duration = total_bytes / (16000 * 2 * 1)  # sample_rate * sample_width * channels
    else:
        logger.warning(f"⚠️ No audio chunks written for session {session_id}")
        duration = 0.0

    logger.info(
        f"🎵 Audio persistence job complete for session {session_id}: "
        f"{conversation_count} conversations, {total_chunk_count} total chunks, "
        f"{total_bytes / 1024 / 1024:.2f} MB, {runtime_seconds:.1f}s runtime"
    )

    # Clean up Redis tracking keys
    audio_job_key = f"audio_persistence:session:{session_id}"
    await redis_client.delete(audio_job_key)
    conversation_key = f"conversation:current:{session_id}"
    await redis_client.delete(conversation_key)
    logger.info(f"🧹 Cleaned up tracking keys for session {session_id}")

    return {
        "session_id": session_id,
        "conversation_count": conversation_count,
        "last_audio_file_path": str(file_path) if file_path else None,
        "total_chunk_count": total_chunk_count,
        "total_bytes": total_bytes,
        "duration_seconds": duration,
        "runtime_seconds": runtime_seconds
    }


# Enqueue wrapper functions

def enqueue_cropping(
    conversation_id: str,
    audio_path: str,
    priority: JobPriority = JobPriority.NORMAL
):
    """
    Enqueue an audio cropping job.

    Args:
        conversation_id: Conversation ID
        audio_path: Path to audio file
        priority: Job priority level

    Returns:
        RQ Job object for tracking.
    """
    timeout_mapping = {
        JobPriority.URGENT: 300,  # 5 minutes
        JobPriority.HIGH: 240,    # 4 minutes
        JobPriority.NORMAL: 180,  # 3 minutes
        JobPriority.LOW: 120      # 2 minutes
    }

    job = default_queue.enqueue(
        process_cropping_job,
        conversation_id,
        audio_path,
        job_timeout=timeout_mapping.get(priority, 180),
        result_ttl=JOB_RESULT_TTL,
        job_id=f"crop_{conversation_id[:12]}",
        description=f"Crop audio for conversation {conversation_id[:12]}",
        meta={'conversation_id': conversation_id}
    )

    logger.info(f"📥 RQ: Enqueued cropping job {job.id} for conversation {conversation_id}")
    return job
