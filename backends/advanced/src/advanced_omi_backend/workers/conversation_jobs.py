"""
Conversation-related RQ job functions.

This module contains jobs related to conversation management and updates.
"""

import asyncio
import logging
import time, os
from datetime import datetime
from typing import Dict, Any
from rq.job import Job
from rq.exceptions import NoSuchJobError

from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.controllers.queue_controller import redis_conn
from advanced_omi_backend.controllers.session_controller import mark_session_complete
from advanced_omi_backend.services.plugin_service import get_plugin_router, init_plugin_router

from advanced_omi_backend.utils.conversation_utils import (
    analyze_speech,
    extract_speakers_from_segments,
    track_speech_activity,
    update_job_progress_metadata,
)
from advanced_omi_backend.utils.conversation_utils import (
    is_meaningful_speech,
    mark_conversation_deleted,
)

from advanced_omi_backend.controllers.queue_controller import start_post_conversation_jobs

logger = logging.getLogger(__name__)


async def handle_end_of_conversation(
    session_id: str,
    conversation_id: str,
    client_id: str,
    user_id: str,
    start_time: float,
    last_result_count: int,
    timeout_triggered: bool,
    redis_client,
    end_reason: str = "unknown",
) -> Dict[str, Any]:
    """
    Handle end-of-conversation cleanup and session restart logic.

    This function is called at the end of open_conversation_job to:
    1. Clean up Redis streams and tracking keys
    2. Increment conversation count for the session
    3. Re-enqueue speech detection job if session is still active
    4. Record conversation end reason in database

    Args:
        session_id: Stream session ID
        conversation_id: Conversation ID that just completed
        client_id: Client ID
        user_id: User ID
        start_time: Job start time (for runtime calculation)
        last_result_count: Number of transcription results processed
        timeout_triggered: Whether closure was due to inactivity timeout
        redis_client: Redis client instance
        end_reason: Reason conversation ended (user_stopped, inactivity_timeout, websocket_disconnect, etc.)

    Returns:
        Dict with conversation_id, conversation_count, final_result_count, runtime_seconds, timeout_triggered, end_reason
    """
    # Clean up Redis streams to prevent memory leaks
    try:
        # NOTE: Do NOT delete audio:stream:{client_id} here!
        # The audio stream is per-client (WebSocket connection), not per-conversation.
        # It's still actively receiving audio and will be reused by the next conversation.
        # Only delete it on WebSocket disconnect (handled in websocket_controller.py)

        # Delete the transcription results stream (per-session/conversation)
        results_stream_key = f"transcription:results:{session_id}"
        await redis_client.delete(results_stream_key)
        logger.info(f"🧹 Deleted results stream: {results_stream_key}")

        # Set TTL on session key (expire after 1 hour)
        session_key = f"audio:session:{session_id}"
        await redis_client.expire(session_key, 3600)
        logger.info(f"⏰ Set TTL on session key: {session_key}")
    except Exception as cleanup_error:
        logger.warning(f"⚠️ Error during stream cleanup: {cleanup_error}")

    # Clean up Redis tracking keys so speech detection job knows conversation is complete
    open_job_key = f"open_conversation:session:{session_id}"
    await redis_client.delete(open_job_key)
    logger.info(f"🧹 Cleaned up tracking key {open_job_key}")

    # Delete the conversation:current signal so audio persistence knows conversation ended
    current_conversation_key = f"conversation:current:{session_id}"
    await redis_client.delete(current_conversation_key)
    logger.info(f"🧹 Deleted conversation:current signal for session {session_id[:12]}")

    # Update conversation in database with end reason and completion time
    from advanced_omi_backend.models.conversation import Conversation
    from datetime import datetime

    conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if conversation:
        # Convert string to enum
        try:
            conversation.end_reason = Conversation.EndReason(end_reason)
        except ValueError:
            logger.warning(f"⚠️ Invalid end_reason '{end_reason}', using UNKNOWN")
            conversation.end_reason = Conversation.EndReason.UNKNOWN

        conversation.completed_at = datetime.utcnow()
        await conversation.save()
        logger.info(f"💾 Saved conversation {conversation_id[:12]} end_reason: {conversation.end_reason}")
    else:
        logger.warning(f"⚠️ Conversation {conversation_id} not found for end reason tracking")

    # Increment conversation count for this session
    conversation_count_key = f"session:conversation_count:{session_id}"
    conversation_count = await redis_client.incr(conversation_count_key)
    await redis_client.expire(conversation_count_key, 3600)  # 1 hour TTL
    logger.info(f"📊 Conversation count for session {session_id}: {conversation_count}")

    # Check if session is still active (user still recording) and restart listening jobs
    session_key = f"audio:session:{session_id}"
    session_status = await redis_client.hget(session_key, "status")
    if session_status:
        status_str = (
            session_status.decode() if isinstance(session_status, bytes) else session_status
        )

        if status_str == "active":
            # Session still active - enqueue new speech detection for next conversation
            logger.info(
                f"🔄 Enqueueing new speech detection (conversation #{conversation_count + 1})"
            )

            from advanced_omi_backend.controllers.queue_controller import (
                transcription_queue,
                redis_conn,
                JOB_RESULT_TTL,
            )
            from advanced_omi_backend.workers.transcription_jobs import stream_speech_detection_job

            # Enqueue speech detection job for next conversation (audio persistence keeps running)
            speech_job = transcription_queue.enqueue(
                stream_speech_detection_job,
                session_id,
                user_id,
                client_id,
                job_timeout=86400,  # 24 hours to match max_runtime in stream_speech_detection_job
                result_ttl=JOB_RESULT_TTL,
                job_id=f"speech-detect_{session_id[:12]}_{conversation_count}",
                description=f"Listening for speech (conversation #{conversation_count + 1})",
                meta={"client_id": client_id, "session_level": True},
            )

            # Store job ID for cleanup (keyed by client_id for WebSocket cleanup)
            try:
                redis_conn.set(f"speech_detection_job:{client_id}", speech_job.id, ex=86400)  # 24 hours
                logger.info(f"📌 Stored speech detection job ID for client {client_id}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to store job ID for {client_id}: {e}")

            logger.info(f"✅ Enqueued speech detection job {speech_job.id}")
        else:
            logger.info(
                f"Session {session_id} status={status_str}, not restarting (user stopped recording)"
            )
    else:
        logger.info(f"Session {session_id} not found, not restarting (session ended)")

    return {
        "conversation_id": conversation_id,
        "conversation_count": conversation_count,
        "deleted": False,  # This conversation was not deleted (normal completion)
        "final_result_count": last_result_count,
        "runtime_seconds": time.time() - start_time,
        "timeout_triggered": timeout_triggered,
        "end_reason": end_reason,
    }


@async_job(redis=True, beanie=True)
async def open_conversation_job(
    session_id: str,
    user_id: str,
    client_id: str,
    speech_detected_at: float,
    speech_job_id: str = None,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    Long-running RQ job that creates and continuously updates conversation with transcription results.

    Creates conversation when speech is detected, then monitors and updates until session ends.

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        speech_detected_at: Timestamp when speech was first detected
        speech_job_id: Optional speech detection job ID to update with conversation_id
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with conversation_id, final_result_count, runtime_seconds

    Note: user_email is fetched from the database when needed.
    """
    from advanced_omi_backend.services.audio_stream import TranscriptionResultsAggregator
    from advanced_omi_backend.models.conversation import Conversation, create_conversation
    from rq import get_current_job

    logger.info(
        f"📝 Creating and opening conversation for session {session_id} (speech detected at {speech_detected_at})"
    )

    # Get current job for meta storage
    current_job = get_current_job()
    current_job.meta = {}
    current_job.save_meta()
    
    # Create minimal streaming conversation (conversation_id auto-generated)
    conversation = create_conversation(
        user_id=user_id,
        client_id=client_id,
        title="Recording...",
        summary="Transcribing audio...",
    )

    # Save to database
    await conversation.insert()
    conversation_id = conversation.conversation_id  # Get the auto-generated ID
    logger.info(f"✅ Created streaming conversation {conversation_id} for session {session_id}")

    # Link job metadata to conversation (cascading updates)
    current_job.meta["conversation_id"] = conversation_id
    current_job.save_meta()

    try:
        speech_job = Job.fetch(speech_job_id, connection=redis_conn)
        speech_job.meta["conversation_id"] = conversation_id
        speech_job.save_meta()
        speaker_check_job_id = speech_job.meta.get("speaker_check_job_id")
        if speaker_check_job_id:
            try:
                speaker_check_job = Job.fetch(speaker_check_job_id, connection=redis_conn)
                speaker_check_job.meta["conversation_id"] = conversation_id
                speaker_check_job.save_meta()
            except Exception as e:
                if isinstance(e, NoSuchJobError):
                    logger.error(
                        f"❌ Missing job hash for speaker_check job {speaker_check_job_id}: "
                        f"Job was linked to speech_job {speech_job_id} but hash key disappeared. "
                        f"This may indicate TTL expiry or job collision."
                    )
                else:
                    raise
    except Exception as e:
        if isinstance(e, NoSuchJobError):
            logger.error(
                f"❌ Missing job hash for speech_job {speech_job_id}: "
                f"Job was created for session {session_id} but hash key disappeared before metadata link. "
                f"This may indicate TTL expiry or job collision."
            )
        else:
            raise
    
    # Signal audio persistence job to rotate to this conversation's file
    rotation_signal_key = f"conversation:current:{session_id}"
    await redis_client.set(rotation_signal_key, conversation_id, ex=86400)  # 24 hour TTL
    logger.info(
        f"🔄 Signaled audio persistence to rotate file for conversation {conversation_id[:12]}"
    )

    # Use redis_client parameter
    aggregator = TranscriptionResultsAggregator(redis_client)

    # Job control
    session_key = f"audio:session:{session_id}"
    max_runtime = 10740  # 3 hours - 60 seconds (single conversations shouldn't exceed 3 hours)
    start_time = time.time()

    last_result_count = 0
    finalize_received = False

    # Inactivity timeout configuration
    inactivity_timeout_seconds = float(os.getenv("SPEECH_INACTIVITY_THRESHOLD_SECONDS", "60"))
    inactivity_timeout_minutes = inactivity_timeout_seconds / 60
    last_meaningful_speech_time = time.time()  # Initialize with conversation start
    timeout_triggered = False  # Track if closure was due to timeout
    last_inactivity_log_time = time.time()  # Track when we last logged inactivity
    last_word_count = 0  # Track word count to detect actual new speech

    # Test mode: wait for audio queue to drain before timing out
    # In real usage, ambient noise keeps connection alive. In tests, chunks arrive in bursts.
    wait_for_queue_drain = os.getenv("WAIT_FOR_AUDIO_QUEUE_DRAIN", "false").lower() == "true"

    logger.info(
        f"📊 Conversation timeout configured: {inactivity_timeout_minutes} minutes ({inactivity_timeout_seconds}s)"
    )
    if wait_for_queue_drain:
        logger.info("🧪 Test mode: Waiting for audio queue to drain before timeout")

    while True:
        # Check if job still exists in Redis (detect zombie state)
        from advanced_omi_backend.utils.job_utils import check_job_alive
        if not await check_job_alive(redis_client, current_job, session_id):
            break

        # Check if session is finalizing (set by producer when recording stops)
        if not finalize_received:
            status = await redis_client.hget(session_key, "status")
            status_str = status.decode() if status else None

            if status_str in ["finalizing", "complete"]:
                finalize_received = True

                # Get completion reason (guaranteed to exist with unified API)
                completion_reason = await redis_client.hget(session_key, "completion_reason")
                completion_reason_str = completion_reason.decode() if completion_reason else "unknown"

                if completion_reason_str == "websocket_disconnect":
                    logger.warning(
                        f"🔌 WebSocket disconnected for session {session_id[:12]} - "
                        f"ending conversation early"
                    )
                    timeout_triggered = False  # This is a disconnect, not a timeout
                else:
                    logger.info(
                        f"🛑 Session finalizing (reason: {completion_reason_str}), "
                        f"waiting for audio persistence job to complete..."
                    )
                break  # Exit immediately when finalize signal received

        # Check max runtime timeout
        if time.time() - start_time > max_runtime:
            logger.warning(f"⏱️ Max runtime reached for {conversation_id}")
            break

        # Get combined results from aggregator
        combined = await aggregator.get_combined_results(session_id)
        current_count = combined["chunk_count"]

        # Analyze speech content using detailed analysis


        transcript_data = {"text": combined["text"], "words": combined.get("words", [])}
        speech_analysis = analyze_speech(transcript_data)

        # Extract speaker information from segments
        segments = combined.get("segments", [])

        # FIX: Validate and filter segments before processing
        validated_segments = []
        for i, seg in enumerate(segments):
            # Check if segment is a dict
            if not isinstance(seg, dict):
                logger.warning(f"Segment {i} is not a dict: {type(seg)}")
                continue

            # Check for required text field
            text = seg.get("text", "").strip()
            if not text:
                logger.debug(f"Segment {i} has no text, skipping")
                continue

            # Check for reasonable timing
            start = seg.get("start", 0.0)
            end = seg.get("end", 0.0)
            if end <= start:
                logger.debug(f"Segment {i} has invalid timing (start={start}, end={end}), correcting")
                # Auto-correct: estimate duration from text length
                estimated_duration = len(text.split()) * 0.5  # ~0.5 seconds per word
                seg["end"] = start + estimated_duration

            # Ensure speaker field exists
            if "speaker" not in seg or not seg["speaker"]:
                seg["speaker"] = "SPEAKER_00"

            validated_segments.append(seg)

        logger.info(f"Validated {len(validated_segments)}/{len(segments)} segments")
        speakers = extract_speakers_from_segments(validated_segments)

        # Track new speech activity (word count based)
        new_speech_time, last_word_count = await track_speech_activity(
            speech_analysis=speech_analysis,
            last_word_count=last_word_count,
            conversation_id=conversation_id,
            redis_client=redis_client,
        )
        if new_speech_time:
            last_meaningful_speech_time = new_speech_time

        # Update job metadata with current progress
        await update_job_progress_metadata(
            current_job=current_job,
            conversation_id=conversation_id,
            session_id=session_id,
            client_id=client_id,
            combined=combined,
            speech_analysis=speech_analysis,
            speakers=speakers,
            last_meaningful_speech_time=last_meaningful_speech_time,
        )

        # Check inactivity timeout and log every 10 seconds
        inactivity_duration = time.time() - last_meaningful_speech_time
        current_time = time.time()

        # Log inactivity every 10 seconds
        if current_time - last_inactivity_log_time >= 10:
            logger.info(
                f"⏱️ Time since last speech: {inactivity_duration:.1f}s (timeout: {inactivity_timeout_seconds:.0f}s)"
            )
            last_inactivity_log_time = current_time

        if inactivity_duration > inactivity_timeout_seconds:
            # In test mode, check if there are pending chunks before timing out
            if wait_for_queue_drain:
                # Check audio persistence queue length
                persist_queue_key = f"audio:queue:{session_id}"
                queue_length = await redis_client.llen(persist_queue_key)

                if queue_length > 0:
                    logger.info(
                        f"🧪 Test mode: Inactivity timeout reached but {queue_length} chunks still in queue, "
                        f"waiting for processing..."
                    )
                    await asyncio.sleep(1)
                    continue

            logger.info(
                f"🕐 Conversation {conversation_id} inactive for "
                f"{inactivity_duration/60:.1f} minutes (threshold: {inactivity_timeout_minutes} min), "
                f"auto-closing conversation (session remains active for next conversation)..."
            )
            # DON'T set session to finalizing - just close this conversation
            # Session remains "active" so new conversations can be created
            # Only user manual stop or WebSocket disconnect should finalize the session
            timeout_triggered = True
            finalize_received = True
            break

        # Track results progress (conversation will get transcript from transcription job)
        if current_count > last_result_count:
            logger.info(
                f"📊 Conversation {conversation_id} progress: "
                f"{current_count} results, {len(combined['text'])} chars, {len(validated_segments)} segments"
            )
            last_result_count = current_count

            # Trigger transcript-level plugins on new transcript segments
            try:
                plugin_router = get_plugin_router()
                if plugin_router:
                    # Get the latest transcript text for plugin processing
                    transcript_text = combined.get('text', '')

                    if transcript_text:
                        plugin_data = {
                            'transcript': transcript_text,
                            'segment_id': f"{session_id}_{current_count}",
                            'conversation_id': conversation_id,
                            'segments': validated_segments,
                            'word_count': speech_analysis.get('word_count', 0),
                        }

                        plugin_results = await plugin_router.dispatch_event(
                            event='transcript.streaming',
                            user_id=user_id,
                            data=plugin_data,
                            metadata={'client_id': client_id}
                        )

                        if plugin_results:
                            logger.info(f"📌 Triggered {len(plugin_results)} streaming transcript plugins")
                            for result in plugin_results:
                                if result.message:
                                    logger.info(f"  Plugin: {result.message}")

                                # If plugin stopped processing, log it
                                if not result.should_continue:
                                    logger.info(f"  Plugin stopped normal processing")

            except Exception as e:
                logger.warning(f"⚠️ Error triggering transcript-level plugins: {e}")

        await asyncio.sleep(1)  # Check every second for responsiveness

    logger.info(
        f"✅ Conversation {conversation_id} updates complete, checking for meaningful speech..."
    )

    # Determine end reason based on how we exited the loop
    # Check session completion_reason from Redis (set by WebSocket controller on disconnect)
    completion_reason = await redis_client.hget(session_key, "completion_reason")
    completion_reason_str = completion_reason.decode() if completion_reason else None

    # Determine end_reason with proper precedence:
    # 1. websocket_disconnect (explicit disconnect from client)
    # 2. inactivity_timeout (no speech for SPEECH_INACTIVITY_THRESHOLD_SECONDS)
    # 3. max_duration (conversation exceeded max runtime)
    # 4. user_stopped (user manually stopped recording)
    if completion_reason_str == "websocket_disconnect":
        end_reason = "websocket_disconnect"
    elif timeout_triggered:
        end_reason = "inactivity_timeout"
    elif time.time() - start_time > max_runtime:
        end_reason = "max_duration"
    else:
        end_reason = "user_stopped"

    logger.info(f"📊 Conversation {conversation_id[:12]} end_reason determined: {end_reason}")

    # FINAL VALIDATION: Check if conversation has meaningful speech before post-processing
    # This prevents empty/noise-only conversations from being processed and saved
    # NOTE: Speech was already validated during streaming, so we skip this check
    # to avoid false negatives from aggregated results lacking proper word-level data
    logger.info("✅ Conversation has meaningful speech (validated during streaming), proceeding with post-processing")

    # Wait for audio_streaming_persistence_job to complete and write MongoDB chunks
    from advanced_omi_backend.utils.audio_chunk_utils import wait_for_audio_chunks

    chunks_ready = await wait_for_audio_chunks(
        conversation_id=conversation_id, max_wait_seconds=30, min_chunks=1
    )

    if not chunks_ready:
        # Mark conversation as deleted - has speech but no audio chunks to process
        await mark_conversation_deleted(
            conversation_id=conversation_id,
            deletion_reason="audio_chunks_not_ready",
        )

        # Call shared cleanup/restart logic before returning
        return await handle_end_of_conversation(
            session_id=session_id,
            conversation_id=conversation_id,
            client_id=client_id,
            user_id=user_id,
            start_time=start_time,
            last_result_count=last_result_count,
            timeout_triggered=timeout_triggered,
            redis_client=redis_client,
            end_reason=end_reason,
        )

    logger.info(f"📦 MongoDB audio chunks ready for conversation {conversation_id[:12]}")

    # Get final streaming transcript and save to conversation
    logger.info(f"📝 Retrieving final streaming transcript for conversation {conversation_id[:12]}")
    final_transcript = await aggregator.get_combined_results(session_id)

    # Fetch conversation from database to ensure we have latest state
    conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if not conversation:
        logger.error(f"❌ Conversation {conversation_id} not found in database")
        raise ValueError(f"Conversation {conversation_id} not found")

    # Create transcript version from streaming results
    version_id = f"streaming_{session_id[:12]}"
    transcript_text = final_transcript.get("text", "")
    segments_data = final_transcript.get("segments", [])

    # If streaming provider didn't provide segments (e.g., Deepgram streaming),
    # create segments from individual final results with word-level data
    if not segments_data:
        logger.info(f"📝 No segments in streaming results, creating from word-level data")
        results = await aggregator.get_session_results(session_id)

        for result in results:
            words = result.get("words", [])
            text = result.get("text", "").strip()

            # Skip empty results or results without timing data
            # WARNING: We don't support results without word-level timing data.
            # Ideally should error, but skipping for now to handle edge cases gracefully.
            if not words or not text:
                continue

            # Create segment dict from this result chunk
            # Each "final" result becomes one segment with generic speaker label
            segment_dict = {
                "start": words[0]["start"],
                "end": words[-1]["end"],
                "text": text,
                "speaker": "SPEAKER_00",  # Generic label, updated by speaker recognition
                "confidence": result.get("confidence"),
                "words": words  # Already in correct format from aggregator
            }
            segments_data.append(segment_dict)

        logger.info(f"✅ Created {len(segments_data)} segments from streaming results")

    # Convert segments to SpeakerSegment format with word-level timestamps
    segments = [
        Conversation.SpeakerSegment(
            start=seg.get("start", 0.0),
            end=seg.get("end", 0.0),
            text=seg.get("text", ""),
            speaker=seg.get("speaker", "SPEAKER_00"),
            confidence=seg.get("confidence"),
            words=[
                Conversation.Word(
                    word=w.get("word", ""),
                    start=w.get("start", 0.0),
                    end=w.get("end", 0.0),
                    confidence=w.get("confidence")
                )
                for w in seg.get("words", [])
            ]
        )
        for seg in segments_data
    ]

    # Determine provider from streaming results
    provider = final_transcript.get("provider", "deepgram")

    # Add streaming transcript as the initial version
    conversation.add_transcript_version(
        version_id=version_id,
        transcript=transcript_text,
        segments=segments,
        provider=provider,
        model=provider,  # Provider name as model
        processing_time_seconds=None,  # Not applicable for streaming
        metadata={
            "source": "streaming",
            "chunk_count": final_transcript.get("chunk_count", 0),
            "word_count": len(final_transcript.get("words", []))
        },
        set_as_active=True
    )

    # Save conversation with streaming transcript
    await conversation.save()
    logger.info(
        f"✅ Saved streaming transcript: {len(transcript_text)} chars, "
        f"{len(segments)} segments, {len(final_transcript.get('words', []))} words "
        f"for conversation {conversation_id[:12]}"
    )

    # Enqueue post-conversation processing pipeline (no batch transcription needed - using streaming transcript)
    client_id = conversation.client_id if conversation else None

    job_ids = start_post_conversation_jobs(
        conversation_id=conversation_id,
        user_id=user_id,
        transcript_version_id=version_id,  # Pass the streaming transcript version ID
        client_id=client_id  # Pass client_id for UI tracking
    )

    logger.info(
        f"📥 Pipeline: speaker({job_ids['speaker_recognition']}) → "
        f"[memory({job_ids['memory']}) + title({job_ids['title_summary']})] → "
        f"event({job_ids['event_dispatch']})"
    )

    # Wait a moment to ensure jobs are registered in RQ
    await asyncio.sleep(0.5)

    # Trigger conversation-level plugins
    try:
        plugin_router = get_plugin_router()
        if plugin_router:
            # Get conversation data for plugin context
            conversation_model = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )

            plugin_data = {
                'conversation': {
                    'conversation_id': conversation_id,
                    'client_id': client_id,
                    'user_id': user_id,
                },
                'transcript': conversation_model.transcript if conversation_model else "",
                'duration': time.time() - start_time,
                'conversation_id': conversation_id,
            }

            plugin_results = await plugin_router.dispatch_event(
                event='conversation.complete',
                user_id=user_id,
                data=plugin_data,
                metadata={'end_reason': end_reason}
            )

            if plugin_results:
                logger.info(f"📌 Triggered {len(plugin_results)} conversation-level plugins")
                for result in plugin_results:
                    if result.message:
                        logger.info(f"  Plugin result: {result.message}")

    except Exception as e:
        logger.warning(f"⚠️ Error triggering conversation-level plugins: {e}")

    # Call shared cleanup/restart logic
    return await handle_end_of_conversation(
        session_id=session_id,
        conversation_id=conversation_id,
        client_id=client_id,
        user_id=user_id,
        start_time=start_time,
        last_result_count=last_result_count,
        timeout_triggered=timeout_triggered,
        redis_client=redis_client,
        end_reason=end_reason,
    )


@async_job(redis=True, beanie=True)
async def generate_title_summary_job(conversation_id: str, *, redis_client=None) -> Dict[str, Any]:
    """
    Generate title, short summary, and detailed summary for a conversation using LLM.

    This job runs independently of transcription and memory jobs to ensure
    conversations always get meaningful titles and summaries, even if other
    processing steps fail.

    Uses the utility functions from conversation_utils for consistent title/summary generation.

    Args:
        conversation_id: Conversation ID
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with generated title, summary, and detailed_summary
    """
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.utils.conversation_utils import (
        generate_title,
        generate_short_summary,
        generate_detailed_summary,
    )

    logger.info(f"📝 Starting title/summary generation for conversation {conversation_id}")

    start_time = time.time()

    # Get the conversation
    conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    # Get transcript and segments (properties return data from active transcript version)
    transcript_text = conversation.transcript or ""
    segments = conversation.segments or []

    if not transcript_text and (not segments or len(segments) == 0):
        logger.warning(f"⚠️ No transcript or segments available for conversation {conversation_id}")
        return {
            "success": False,
            "error": "No transcript or segments available",
            "conversation_id": conversation_id,
        }

    # Generate title, short summary, and detailed summary using unified utilities
    try:
        logger.info(
            f"🤖 Generating title/summary/detailed_summary using LLM for conversation {conversation_id}"
        )

        # Convert segments to dict format expected by utils
        segment_dicts = None
        if segments and len(segments) > 0:
            segment_dicts = [
                {"speaker": seg.speaker, "text": seg.text, "start": seg.start, "end": seg.end}
                for seg in segments
            ]

        # Generate all three summaries in parallel for efficiency
        import asyncio

        title, short_summary, detailed_summary = await asyncio.gather(
            generate_title(transcript_text, segments=segment_dicts),
            generate_short_summary(transcript_text, segments=segment_dicts),
            generate_detailed_summary(transcript_text, segments=segment_dicts),
        )

        conversation.title = title
        conversation.summary = short_summary
        conversation.detailed_summary = detailed_summary

        logger.info(f"✅ Generated title: '{conversation.title}'")
        logger.info(f"✅ Generated summary: '{conversation.summary}'")
        logger.info(f"✅ Generated detailed summary: {len(conversation.detailed_summary)} chars")

    except Exception as gen_error:
        logger.error(f"❌ Title/summary generation failed: {gen_error}")
        return {
            "success": False,
            "error": str(gen_error),
            "conversation_id": conversation_id,
            "processing_time_seconds": time.time() - start_time,
        }

    # Save the updated conversation
    await conversation.save()

    processing_time = time.time() - start_time

    # Update job metadata
    from rq import get_current_job

    current_job = get_current_job()
    if current_job:
        if not current_job.meta:
            current_job.meta = {}
        current_job.meta.update(
            {
                "conversation_id": conversation_id,
                "title": conversation.title,
                "summary": conversation.summary,
                "detailed_summary_length": (
                    len(conversation.detailed_summary) if conversation.detailed_summary else 0
                ),
                "segment_count": len(segments),
                "processing_time": processing_time,
            }
        )
        current_job.save_meta()

    logger.info(
        f"✅ Title/summary generation completed for {conversation_id} in {processing_time:.2f}s"
    )

    return {
        "success": True,
        "conversation_id": conversation_id,
        "title": conversation.title,
        "summary": conversation.summary,
        "detailed_summary": conversation.detailed_summary,
        "processing_time_seconds": processing_time,
    }


@async_job(redis=True, beanie=True)
async def dispatch_conversation_complete_event_job(
    conversation_id: str,
    client_id: str,
    user_id: str,
    *,
    redis_client=None
) -> Dict[str, Any]:
    """
    Dispatch conversation.complete plugin event for file upload processing.

    This job runs at the end of the post-conversation job chain to ensure
    plugins receive the conversation.complete event for uploaded audio files.
    WebSocket streaming dispatches this event in open_conversation_job instead.

    Args:
        conversation_id: Conversation ID
        client_id: Client ID
        user_id: User ID
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with success status and plugin results
    """
    from advanced_omi_backend.models.conversation import Conversation

    logger.info(f"📌 Dispatching conversation.complete event for conversation {conversation_id}")

    start_time = time.time()

    # Get the conversation to include in event data
    conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    # Get user email for event data
    from advanced_omi_backend.models.user import User
    user = await User.get(user_id)
    user_email = user.email if user else ""

    # Prepare plugin event data (same format as open_conversation_job)
    try:
        # Get or initialize plugin router (same pattern as transcription_jobs.py)
        plugin_router = get_plugin_router()
        if not plugin_router:
            logger.info("🔧 Initializing plugin router in worker process...")
            plugin_router = init_plugin_router()

            # Initialize all plugins asynchronously (same as app_factory.py)
            if plugin_router:
                for plugin_id, plugin in plugin_router.plugins.items():
                    try:
                        await plugin.initialize()
                        logger.info(f"✅ Plugin '{plugin_id}' initialized")
                    except Exception as e:
                        logger.error(f"Failed to initialize plugin '{plugin_id}': {e}")

        if not plugin_router:
            logger.warning("⚠️ Plugin router could not be initialized, skipping event dispatch")
            return {"success": True, "skipped": True, "reason": "No plugin router"}

        plugin_data = {
            'conversation': {
                'client_id': client_id,
                'user_id': user_id,
            },
            'transcript': conversation.transcript if conversation else "",
            'duration': 0,  # Duration not tracked for file uploads
            'conversation_id': conversation_id,
        }

        plugin_results = await plugin_router.dispatch_event(
            event='conversation.complete',
            user_id=user_id,
            data=plugin_data,
            metadata={'end_reason': 'file_upload'}
        )

        if plugin_results:
            logger.info(f"📌 Triggered {len(plugin_results)} conversation-level plugins")
            for result in plugin_results:
                if result.message:
                    logger.info(f"  Plugin result: {result.message}")

        processing_time = time.time() - start_time
        logger.info(
            f"✅ Conversation complete event dispatched for {conversation_id} in {processing_time:.2f}s"
        )

        return {
            "success": True,
            "conversation_id": conversation_id,
            "plugin_count": len(plugin_results) if plugin_results else 0,
            "processing_time_seconds": processing_time,
        }

    except Exception as e:
        logger.warning(f"⚠️ Error dispatching conversation complete event: {e}")
        return {
            "success": False,
            "error": str(e),
            "conversation_id": conversation_id,
        }
