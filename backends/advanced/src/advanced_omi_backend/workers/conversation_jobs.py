"""
Conversation-related RQ job functions.

This module contains jobs related to conversation management and updates.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from rq.exceptions import NoSuchJobError
from rq.job import Job

from advanced_omi_backend.controllers.queue_controller import (
    redis_conn,
    start_post_conversation_jobs,
)
from advanced_omi_backend.controllers.session_controller import mark_session_complete
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.observability.otel_setup import set_otel_session
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.services.plugin_service import (
    dispatch_plugin_event,
    get_plugin_router,
)
from advanced_omi_backend.utils.conversation_utils import (
    analyze_speech,
    extract_speakers_from_segments,
    is_meaningful_speech,
    mark_conversation_deleted,
    track_speech_activity,
    update_job_progress_metadata,
)
from advanced_omi_backend.utils.job_utils import update_job_meta

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
    from datetime import datetime

    from advanced_omi_backend.models.conversation import Conversation

    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if conversation:
        # Convert string to enum
        try:
            conversation.end_reason = Conversation.EndReason(end_reason)
        except ValueError:
            logger.warning(f"⚠️ Invalid end_reason '{end_reason}', using UNKNOWN")
            conversation.end_reason = Conversation.EndReason.UNKNOWN

        conversation.completed_at = datetime.utcnow()
        await conversation.save()
        logger.info(
            f"💾 Saved conversation {conversation_id[:12]} end_reason: {conversation.end_reason}"
        )
    else:
        logger.warning(
            f"⚠️ Conversation {conversation_id} not found for end reason tracking"
        )

    # Increment conversation count for this session
    conversation_count_key = f"session:conversation_count:{session_id}"
    conversation_count = await redis_client.incr(conversation_count_key)
    await redis_client.expire(conversation_count_key, 3600)  # 1 hour TTL
    logger.info(f"📊 Conversation count for session {session_id}: {conversation_count}")

    # Check if session is still active (user still recording) and restart listening jobs
    session_key = f"audio:session:{session_id}"
    # Fetch both status and websocket_connected in one Redis call
    status_raw, ws_connected_raw = await redis_client.hmget(
        session_key, "status", "websocket_connected"
    )

    if status_raw:
        status_str = (
            status_raw.decode() if isinstance(status_raw, bytes) else status_raw
        )
        ws_connected = (
            ws_connected_raw.decode()
            if isinstance(ws_connected_raw, bytes)
            else (ws_connected_raw or "false")
        ) == "true"

        # Determine if we should restart speech detection
        should_restart = False
        if status_str == "active":
            should_restart = True
        elif ws_connected:
            # Race condition recovery: WebSocket is still connected but status got
            # corrupted (e.g., status endpoint polling set "finished" during the
            # inter-conversation gap). Reset status and restart anyway.
            logger.warning(
                f"⚠️ Race condition recovery for session {session_id[:12]}: "
                f"status={status_str} but websocket_connected=true. "
                f"Resetting status to 'active' and restarting speech detection."
            )
            await redis_client.hset(session_key, "status", "active")
            should_restart = True

        if should_restart:
            # Session still active - enqueue new speech detection for next conversation
            logger.info(
                f"🔄 Enqueueing new speech detection (conversation #{conversation_count + 1})"
            )

            # Clear transcription completion flag so streaming consumer can re-attach
            # (if it exited during previous conversation, this flag prevents re-discovery)
            completion_key = f"transcription:complete:{session_id}"
            await redis_client.delete(completion_key)
            logger.info(f"🧹 Cleared transcription completion flag: {completion_key}")

            from advanced_omi_backend.controllers.queue_controller import (
                JOB_RESULT_TTL,
                redis_conn,
                transcription_queue,
            )
            from advanced_omi_backend.workers.transcription_jobs import (
                stream_speech_detection_job,
            )

            # Enqueue speech detection job for next conversation (audio persistence keeps running)
            speech_job = transcription_queue.enqueue(
                stream_speech_detection_job,
                session_id,
                user_id,
                client_id,
                job_timeout=86400,  # 24 hours to match max_runtime in stream_speech_detection_job
                result_ttl=JOB_RESULT_TTL,
                job_id=f"speech-detect_{session_id}_{conversation_count}",
                description=f"Listening for speech (conversation #{conversation_count + 1})",
                meta={"client_id": client_id, "session_level": True},
            )

            # Store job ID for cleanup (keyed by client_id for WebSocket cleanup)
            try:
                redis_conn.set(
                    f"speech_detection_job:{client_id}", speech_job.id, ex=86400
                )  # 24 hours
                logger.info(f"📌 Stored speech detection job ID for client {client_id}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to store job ID for {client_id}: {e}")

            logger.info(f"✅ Enqueued speech detection job {speech_job.id}")
        else:
            logger.info(
                f"Session {session_id} status={status_str}, ws_connected={ws_connected}, "
                f"not restarting (user stopped recording)"
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


@dataclass
class ConversationState:
    """Mutable state tracked across the conversation monitoring loop."""

    conversation_id: str = ""
    session_id: str = ""
    user_id: str = ""
    client_id: str = ""
    start_time: float = 0.0
    last_result_count: int = 0
    timeout_triggered: bool = False
    close_requested_reason: Optional[str] = None
    last_meaningful_speech_time: float = 0.0
    last_word_count: int = 0
    end_reason: str = "unknown"


def _validate_segments(segments: list) -> list:
    """Validate and filter transcription segments, correcting minor issues.

    Filters out non-dict segments and segments with no text. Corrects invalid
    timing (end <= start) by estimating duration from word count. Ensures
    speaker field is always a non-empty string.
    """
    validated = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            logger.warning(f"Segment {i} is not a dict: {type(seg)}")
            continue

        text = seg.get("text", "").strip()
        if not text:
            logger.debug(f"Segment {i} has no text, skipping")
            continue

        start = seg.get("start", 0.0)
        end = seg.get("end", 0.0)
        if end <= start:
            logger.debug(
                f"Segment {i} has invalid timing (start={start}, end={end}), correcting"
            )
            estimated_duration = len(text.split()) * 0.5  # ~0.5 seconds per word
            seg["end"] = start + estimated_duration

        speaker = seg.get("speaker")
        if speaker is None or speaker == "":
            seg["speaker"] = "SPEAKER_00"
        elif isinstance(speaker, (int, float)):
            seg["speaker"] = f"Speaker {int(speaker)}"

        validated.append(seg)

    logger.info(f"Validated {len(validated)}/{len(segments)} segments")
    return validated


async def _initialize_conversation(
    session_id: str,
    user_id: str,
    client_id: str,
    speech_job_id: str,
    current_job,
    redis_client,
) -> str:
    """Create or reuse a conversation for this session.

    Checks for an existing placeholder conversation in Redis. If found and valid,
    reuses it. Otherwise creates a new conversation. Attaches session markers,
    links job metadata, and signals audio persistence to rotate files.

    Returns:
        conversation_id of the created/reused conversation.
    """
    from advanced_omi_backend.models.conversation import (
        Conversation,
        create_conversation,
    )

    # Check if a placeholder conversation already exists for this session
    conversation_key = f"conversation:current:{session_id}"
    existing_conversation_id_bytes = await redis_client.get(conversation_key)

    logger.info(
        f"🔍 Checking for placeholder: key={conversation_key}, found={existing_conversation_id_bytes is not None}"
    )

    conversation = None
    if existing_conversation_id_bytes:
        existing_conversation_id = existing_conversation_id_bytes.decode()
        logger.info(
            f"🔍 Found Redis key with conversation_id={existing_conversation_id}"
        )

        # Try to fetch the existing conversation by conversation_id
        conversation = await Conversation.find_one(
            Conversation.conversation_id == existing_conversation_id
        )

        if conversation:
            always_persist = getattr(conversation, "always_persist", False)
            processing_status = getattr(conversation, "processing_status", None)
            logger.info(
                f"🔍 Found conversation in DB: always_persist={always_persist}, "
                f"processing_status={processing_status}"
            )
        else:
            logger.warning(
                f"⚠️ Conversation {existing_conversation_id} not found in database!"
            )

        # Verify it's a placeholder conversation (always_persist=True, processing_status='pending_transcription')
        if (
            conversation
            and getattr(conversation, "always_persist", False)
            and getattr(conversation, "processing_status", None)
            == "pending_transcription"
        ):
            logger.info(
                f"🔄 Reusing placeholder conversation {conversation.conversation_id} for session {session_id}"
            )
            # Update placeholder with active recording status
            conversation.title = "Recording..."
            conversation.summary = "Transcribing audio..."
            await conversation.save()
            conversation_id = conversation.conversation_id
        else:
            if conversation:
                logger.info(
                    f"⚠️ Found conversation {existing_conversation_id} but not a valid placeholder "
                    f"(always_persist={getattr(conversation, 'always_persist', False)}, "
                    f"processing_status={getattr(conversation, 'processing_status', None)}), creating new"
                )
            conversation = None
    else:
        logger.info(
            f"🔍 No Redis key found for {conversation_key}, creating new conversation"
        )

    # If no valid placeholder found, create new conversation
    if not conversation:
        conversation = create_conversation(
            user_id=user_id,
            client_id=client_id,
            title="Recording...",
            summary="Transcribing audio...",
        )
        await conversation.insert()
        conversation_id = conversation.conversation_id
        logger.info(
            f"✅ Created streaming conversation {conversation_id} for session {session_id}"
        )

    # Attach markers from Redis session (e.g., button events captured during streaming)
    session_key = f"audio:session:{session_id}"
    markers_json = await redis_client.hget(session_key, "markers")
    if markers_json:
        try:
            markers_data = (
                markers_json if isinstance(markers_json, str) else markers_json.decode()
            )
            conversation.markers = json.loads(markers_data)
            await conversation.save()
            logger.info(
                f"📌 Attached {len(conversation.markers)} markers to conversation {conversation_id}"
            )
        except Exception as marker_err:
            logger.warning(f"⚠️ Failed to parse markers from Redis: {marker_err}")

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
                speaker_check_job = Job.fetch(
                    speaker_check_job_id, connection=redis_conn
                )
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
    await redis_client.set(
        rotation_signal_key, conversation_id, ex=86400
    )  # 24 hour TTL
    logger.info(
        f"🔄 Signaled audio persistence to rotate file for conversation {conversation_id[:12]}"
    )

    return conversation_id


async def _monitor_conversation_loop(
    state: ConversationState,
    aggregator,
    current_job,
    redis_client,
) -> None:
    """Poll transcription results and track conversation activity until exit.

    Runs the main monitoring loop that:
    - Detects zombie jobs (job hash missing from Redis)
    - Handles session finalize signals (disconnect, user stop)
    - Handles conversation close requests (API, plugin, button)
    - Polls the transcription aggregator for new results
    - Validates segments and tracks speech activity
    - Detects inactivity timeout and closes conversation
    - Dispatches transcript.streaming plugin events

    Mutates ``state`` in place with final values for timeout_triggered,
    close_requested_reason, last_result_count, and last_word_count.
    """
    session_key = f"audio:session:{state.session_id}"
    max_runtime = (
        10740  # 3 hours - 60 seconds (single conversations shouldn't exceed 3 hours)
    )

    finalize_received = False

    # Inactivity timeout configuration
    inactivity_timeout_seconds = float(
        os.getenv("SPEECH_INACTIVITY_THRESHOLD_SECONDS", "60")
    )
    inactivity_timeout_minutes = inactivity_timeout_seconds / 60
    last_inactivity_log_time = (
        time.time()
    )  # Track when we last logged inactivity (wall-clock for logging)

    # Test mode: wait for audio queue to drain before timing out
    # In real usage, ambient noise keeps connection alive. In tests, chunks arrive in bursts.
    wait_for_queue_drain = (
        os.getenv("WAIT_FOR_AUDIO_QUEUE_DRAIN", "false").lower() == "true"
    )

    logger.info(
        f"📊 Conversation timeout configured: {inactivity_timeout_minutes} minutes ({inactivity_timeout_seconds}s)"
    )
    if wait_for_queue_drain:
        logger.info("🧪 Test mode: Waiting for audio queue to drain before timeout")

    while True:
        # Check if job still exists in Redis (detect zombie state)
        from advanced_omi_backend.utils.job_utils import check_job_alive

        if not await check_job_alive(redis_client, current_job, state.session_id):
            break

        # Check if session is finalizing (set by producer when recording stops)
        if not finalize_received:
            # Fetch status, completion_reason, and websocket_connected in one call
            status_raw, reason_raw, ws_raw = await redis_client.hmget(
                session_key, "status", "completion_reason", "websocket_connected"
            )
            status_str = status_raw.decode() if status_raw else None
            completion_reason_str = reason_raw.decode() if reason_raw else "unknown"
            ws_connected = (ws_raw.decode() if ws_raw else "false") == "true"

            if status_str in ["finalizing", "finished"]:
                # Check for spurious "finished" from status endpoint race condition:
                # If status is "finished" but WebSocket is still connected and reason
                # is "all_jobs_complete", this was set during the inter-conversation gap.
                # Reset to "active" and continue monitoring.
                if (
                    status_str == "finished"
                    and ws_connected
                    and completion_reason_str == "all_jobs_complete"
                ):
                    logger.warning(
                        f"⚠️ Ignoring spurious 'finished' for session {state.session_id[:12]}: "
                        f"websocket_connected=true, reason=all_jobs_complete. "
                        f"Resetting status to 'active' and continuing."
                    )
                    await redis_client.hset(session_key, "status", "active")
                    # Do NOT break - continue monitoring
                else:
                    finalize_received = True

                    if completion_reason_str == "websocket_disconnect":
                        logger.warning(
                            f"🔌 WebSocket disconnected for session {state.session_id[:12]} - "
                            f"ending conversation early"
                        )
                        state.timeout_triggered = (
                            False  # This is a disconnect, not a timeout
                        )
                    else:
                        logger.info(
                            f"🛑 Session finalizing (reason: {completion_reason_str}), "
                            f"waiting for audio persistence job to complete..."
                        )
                    break  # Exit immediately when finalize signal received

        # Check for conversation close request (set by API, plugins, button press)
        if not finalize_received:
            close_reason = await redis_client.hget(
                session_key, "conversation_close_requested"
            )
            if close_reason:
                await redis_client.hdel(session_key, "conversation_close_requested")
                state.close_requested_reason = (
                    close_reason.decode()
                    if isinstance(close_reason, bytes)
                    else close_reason
                )
                logger.info(
                    f"🔒 Conversation close requested: {state.close_requested_reason}"
                )
                state.timeout_triggered = True  # Session stays active (same restart behavior as inactivity timeout)
                finalize_received = True
                break

        # Check max runtime timeout
        if time.time() - state.start_time > max_runtime:
            logger.warning(f"⏱️ Max runtime reached for {state.conversation_id}")
            break

        # Get combined results from aggregator
        combined = await aggregator.get_combined_results(state.session_id)
        current_count = combined["chunk_count"]

        # Analyze speech content using detailed analysis
        transcript_data = {"text": combined["text"], "words": combined.get("words", [])}
        speech_analysis = analyze_speech(transcript_data)

        # Extract speaker information from segments
        segments = combined.get("segments", [])

        # Validate and filter segments before processing
        validated_segments = _validate_segments(segments)
        speakers = extract_speakers_from_segments(validated_segments)

        # Track new speech activity (word count based)
        new_speech_time, state.last_word_count = await track_speech_activity(
            speech_analysis=speech_analysis,
            last_word_count=state.last_word_count,
            conversation_id=state.conversation_id,
            redis_client=redis_client,
        )
        if new_speech_time:
            state.last_meaningful_speech_time = new_speech_time

        # Update job metadata with current progress
        await update_job_progress_metadata(
            current_job=current_job,
            conversation_id=state.conversation_id,
            session_id=state.session_id,
            client_id=state.client_id,
            combined=combined,
            speech_analysis=speech_analysis,
            speakers=speakers,
            last_meaningful_speech_time=state.last_meaningful_speech_time,
        )

        # Check inactivity timeout using audio time (not wall-clock time)
        # Get current audio time from latest transcription
        current_audio_time = speech_analysis.get("speech_end", 0.0)

        # Calculate inactivity based on audio timestamps
        # Only check if we have valid audio timing data
        if current_audio_time > 0 and state.last_meaningful_speech_time > 0:
            inactivity_duration = current_audio_time - state.last_meaningful_speech_time
        else:
            # Fallback: No audio timestamps available (text-only transcription)
            # Can't reliably detect inactivity, so skip timeout check this iteration
            inactivity_duration = 0
            if speech_analysis.get("fallback", False):
                logger.debug(
                    "⚠️ Skipping inactivity check (no audio timestamps available)"
                )

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
                persist_queue_key = f"audio:queue:{state.session_id}"
                queue_length = await redis_client.llen(persist_queue_key)

                if queue_length > 0:
                    logger.info(
                        f"🧪 Test mode: Inactivity timeout reached but {queue_length} chunks still in queue, "
                        f"waiting for processing..."
                    )
                    await asyncio.sleep(1)
                    continue

            logger.info(
                f"🕐 Conversation {state.conversation_id} inactive for "
                f"{inactivity_duration/60:.1f} minutes (threshold: {inactivity_timeout_minutes} min), "
                f"auto-closing conversation (session remains active for next conversation)..."
            )
            # DON'T set session to finalizing - just close this conversation
            # Session remains "active" so new conversations can be created
            # Only user manual stop or WebSocket disconnect should finalize the session
            state.timeout_triggered = True
            finalize_received = True
            break

        # Track results progress (conversation will get transcript from transcription job)
        if current_count > state.last_result_count:
            logger.info(
                f"📊 Conversation {state.conversation_id} progress: "
                f"{current_count} results, {len(combined['text'])} chars, {len(validated_segments)} segments"
            )
            state.last_result_count = current_count

            # Trigger transcript-level plugins on new transcript segments
            try:
                plugin_router = get_plugin_router()
                if plugin_router:
                    # Get the latest transcript text for plugin processing
                    transcript_text = combined.get("text", "")

                    if transcript_text:
                        plugin_data = {
                            "transcript": transcript_text,
                            "segment_id": f"{state.session_id}_{current_count}",
                            "conversation_id": state.conversation_id,
                            "segments": validated_segments,
                            "word_count": speech_analysis.get("word_count", 0),
                        }

                        logger.info(
                            f"🔌 DISPATCH: transcript.streaming event "
                            f"(conversation={state.conversation_id[:12]}, segment_id={state.session_id}_{current_count})"
                        )

                        plugin_results = await plugin_router.dispatch_event(
                            event=PluginEvent.TRANSCRIPT_STREAMING,
                            user_id=state.user_id,
                            data=plugin_data,
                            metadata={"client_id": state.client_id},
                        )

                        logger.info(
                            f"🔌 RESULT: transcript.streaming dispatched to {len(plugin_results) if plugin_results else 0} plugins"
                        )

                        if plugin_results:
                            logger.info(
                                f"📌 Triggered {len(plugin_results)} streaming transcript plugins"
                            )
                            for result in plugin_results:
                                if result.message:
                                    logger.info(f"  Plugin: {result.message}")

                                # If plugin stopped processing, log it
                                if not result.should_continue:
                                    logger.info(f"  Plugin stopped normal processing")

            except Exception as e:
                logger.warning(f"⚠️ Error triggering transcript-level plugins: {e}")

        await asyncio.sleep(1)  # Check every second for responsiveness


async def _save_streaming_transcript(
    session_id: str,
    conversation_id: str,
    aggregator,
) -> str:
    """Retrieve final streaming transcript and save it to the conversation document.

    Gets the combined transcription results from the aggregator, converts them
    to Word and SpeakerSegment model objects, creates a transcript version, and
    saves to MongoDB.

    Returns:
        version_id of the saved transcript version.
    """
    from advanced_omi_backend.models.conversation import Conversation

    logger.info(
        f"📝 Retrieving final streaming transcript for conversation {conversation_id[:12]}"
    )
    final_transcript = await aggregator.get_combined_results(session_id)

    # Fetch conversation from database to ensure we have latest state
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"❌ Conversation {conversation_id} not found in database")
        raise ValueError(f"Conversation {conversation_id} not found")

    # Create transcript version from streaming results
    version_id = f"streaming_{session_id[:12]}"
    transcript_text = final_transcript.get("text", "")
    words_data = final_transcript.get("words", [])  # All words from aggregator

    # Convert words to Word objects (including per-word speaker labels if present)
    words = [
        Conversation.Word(
            word=w.get("word", ""),
            start=w.get("start", 0.0),
            end=w.get("end", 0.0),
            confidence=w.get("confidence"),
            speaker=w.get("speaker"),
            speaker_confidence=w.get("speaker_confidence"),
        )
        for w in words_data
    ]

    # Use provider-supplied segments if available (from streaming diarization),
    # otherwise leave empty for speaker recognition service to fill later.
    segments_data = final_transcript.get("segments", [])
    if segments_data:
        segments = [
            Conversation.SpeakerSegment(
                start=s.get("start", 0.0),
                end=s.get("end", 0.0),
                text=s.get("text", ""),
                speaker=str(s.get("speaker", "Unknown")),
                words=[
                    Conversation.Word(
                        word=sw.get("word", ""),
                        start=sw.get("start", 0.0),
                        end=sw.get("end", 0.0),
                        confidence=sw.get("confidence"),
                        speaker=sw.get("speaker"),
                        speaker_confidence=sw.get("speaker_confidence"),
                    )
                    for sw in s.get("words", [])
                ],
            )
            for s in segments_data
        ]
    else:
        segments = []

    # Determine provider from streaming results
    provider = final_transcript.get("provider", "deepgram")

    # Determine diarization source if provider supplied segments
    diarization_source = "provider" if segments else None

    # Add streaming transcript with words at version level
    version = conversation.add_transcript_version(
        version_id=version_id,
        transcript=transcript_text,
        words=words,  # Store at version level
        segments=segments,  # Provider segments or empty (filled by speaker service later)
        provider=provider,
        model=provider,  # Provider name as model
        processing_time_seconds=None,  # Not applicable for streaming
        metadata={
            "source": "streaming",
            "chunk_count": final_transcript.get("chunk_count", 0),
            "word_count": len(words),
            "provider_capabilities": {"diarization": bool(segments)},
        },
        set_as_active=True,
    )
    version.diarization_source = diarization_source

    # Update placeholder conversation if it exists
    if (
        getattr(conversation, "always_persist", False)
        and getattr(conversation, "processing_status", None) == "pending_transcription"
    ):
        # Keep placeholder status - will be updated by title_summary_job
        logger.info(
            f"📝 Placeholder conversation {conversation_id} has transcript, "
            f"waiting for title/summary generation"
        )

    # Save conversation with streaming transcript
    await conversation.save()
    segment_info = (
        f"{len(segments)} provider segments (diarization_source={diarization_source})"
        if segments
        else "0 segments (pending speaker recognition)"
    )
    logger.info(
        f"✅ Saved streaming transcript: {len(transcript_text)} chars, "
        f"{segment_info}, {len(words)} words "
        f"for conversation {conversation_id[:12]}"
    )

    return version_id


async def _enqueue_post_processing(
    conversation_id: str,
    user_id: str,
    client_id: str,
    version_id: str,
    end_reason: str,
) -> None:
    """Enqueue post-conversation processing jobs (speaker, memory, title, events).

    Checks configuration for always_batch_retranscribe. If enabled, enqueues
    a batch transcription job first with post-processing depending on it.
    Otherwise starts post-processing immediately with the streaming transcript.
    """
    from advanced_omi_backend.config_loader import get_backend_config

    transcription_cfg = get_backend_config("transcription")
    batch_retranscribe = False
    if transcription_cfg:
        from omegaconf import OmegaConf

        cfg_dict = OmegaConf.to_container(transcription_cfg, resolve=True)
        batch_retranscribe = cfg_dict.get("always_batch_retranscribe", False)

    if batch_retranscribe:
        # BATCH PATH: Streaming transcript saved as preview — user sees it immediately
        # Full post-processing (speaker, memory, title) waits for batch transcript
        from advanced_omi_backend.config import get_transcription_job_timeout
        from advanced_omi_backend.controllers.queue_controller import (
            JOB_RESULT_TTL,
            transcription_queue,
        )
        from advanced_omi_backend.workers.transcription_jobs import (
            transcribe_full_audio_job,
        )

        batch_version_id = f"batch_{conversation_id[:12]}"
        batch_job = transcription_queue.enqueue(
            transcribe_full_audio_job,
            conversation_id,
            batch_version_id,
            "always_batch_retranscribe",
            job_timeout=get_transcription_job_timeout(),
            result_ttl=JOB_RESULT_TTL,
            job_id=f"batch_retranscribe_{conversation_id[:12]}",
            description=f"Batch re-transcription for {conversation_id[:8]}",
            meta={"conversation_id": conversation_id, "client_id": client_id},
        )

        logger.info(
            f"🔄 Batch re-transcribe enabled: enqueued batch job {batch_job.id} "
            f"(streaming transcript is preview only)"
        )

        # Run post-processing ONLY after batch completes
        job_ids = start_post_conversation_jobs(
            conversation_id=conversation_id,
            user_id=user_id,
            transcript_version_id=batch_version_id,
            depends_on_job=batch_job,
            client_id=client_id,
            end_reason=end_reason,
        )

        logger.info(
            f"📥 Pipeline: batch_retranscribe({batch_job.id}) → "
            f"speaker({job_ids['speaker_recognition']}) → "
            f"[memory({job_ids['memory']}) + title({job_ids['title_summary']})] → "
            f"event({job_ids['event_dispatch']})"
        )
    else:
        # NORMAL PATH: Process streaming transcript immediately (existing behavior)
        job_ids = start_post_conversation_jobs(
            conversation_id=conversation_id,
            user_id=user_id,
            transcript_version_id=version_id,  # Pass the streaming transcript version ID
            depends_on_job=None,  # No dependency - streaming already succeeded
            client_id=client_id,  # Pass client_id for UI tracking
            end_reason=end_reason,  # Pass the determined end_reason (websocket_disconnect, inactivity_timeout, etc.)
        )

        logger.info(
            f"📥 Pipeline: speaker({job_ids['speaker_recognition']}) → "
            f"[memory({job_ids['memory']}) + title({job_ids['title_summary']})] → "
            f"event({job_ids['event_dispatch']})"
        )

    # Wait a moment to ensure jobs are registered in RQ
    await asyncio.sleep(0.5)

    logger.info(
        f"✅ Post-conversation pipeline started with event dispatch job (end_reason={end_reason})"
    )


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
    from rq import get_current_job

    from advanced_omi_backend.services.audio_stream import (
        TranscriptionResultsAggregator,
    )

    logger.info(
        f"📝 Creating and opening conversation for session {session_id} (speech detected at {speech_detected_at})"
    )

    # Phase 1: Initialize job and conversation
    current_job = get_current_job()
    current_job.meta = {}
    current_job.save_meta()

    conversation_id = await _initialize_conversation(
        session_id=session_id,
        user_id=user_id,
        client_id=client_id,
        speech_job_id=speech_job_id,
        current_job=current_job,
        redis_client=redis_client,
    )

    # Phase 2: Monitor conversation (polling loop)
    aggregator = TranscriptionResultsAggregator(redis_client)
    state = ConversationState(
        conversation_id=conversation_id,
        session_id=session_id,
        user_id=user_id,
        client_id=client_id,
        start_time=time.time(),
    )

    await _monitor_conversation_loop(state, aggregator, current_job, redis_client)

    logger.info(
        f"✅ Conversation {conversation_id} updates complete, checking for meaningful speech..."
    )

    # Phase 3: Determine end reason
    session_key = f"audio:session:{session_id}"
    completion_reason = await redis_client.hget(session_key, "completion_reason")
    completion_reason_str = completion_reason.decode() if completion_reason else None

    if completion_reason_str:
        state.end_reason = completion_reason_str
        logger.info(f"📊 Using completion_reason from session: {state.end_reason}")
    elif state.close_requested_reason:
        state.end_reason = "close_requested"
        logger.info(
            f"📊 Conversation closed by request: {state.close_requested_reason}"
        )
    elif state.timeout_triggered:
        state.end_reason = "inactivity_timeout"
    elif time.time() - state.start_time > 10740:
        state.end_reason = "max_duration"
    else:
        state.end_reason = "user_stopped"

    logger.info(
        f"📊 Conversation {conversation_id[:12]} end_reason determined: {state.end_reason}"
    )

    # Phase 4-7: Post-processing (wrapped in try/finally for guaranteed cleanup)
    end_of_conversation_handled = False
    try:
        logger.info(
            "✅ Conversation has meaningful speech (validated during streaming), proceeding with post-processing"
        )

        # Phase 4: Wait for streaming transcription to complete
        if state.close_requested_reason:
            logger.info(
                f"⏩ Skipping transcription:complete wait for close_requested "
                f"(reason={state.close_requested_reason})"
            )
        else:
            completion_key = f"transcription:complete:{session_id}"
            max_wait_streaming = 30  # seconds
            waited_streaming = 0.0
            while waited_streaming < max_wait_streaming:
                completion_status = await redis_client.get(completion_key)
                if completion_status:
                    status_str = (
                        completion_status.decode()
                        if isinstance(completion_status, bytes)
                        else completion_status
                    )
                    if status_str == "error":
                        logger.warning(
                            f"⚠️ Streaming transcription ended with error for {session_id}, proceeding anyway"
                        )
                    else:
                        logger.info(
                            f"✅ Streaming transcription confirmed complete for {session_id}"
                        )
                    break
                await asyncio.sleep(0.5)
                waited_streaming += 0.5

            if waited_streaming >= max_wait_streaming:
                logger.warning(
                    f"⚠️ Timed out waiting for streaming completion signal for {session_id} "
                    f"(waited {max_wait_streaming}s), proceeding with available transcript"
                )

        # Phase 5: Wait for audio chunks in MongoDB
        from advanced_omi_backend.utils.audio_chunk_utils import wait_for_audio_chunks

        chunks_ready = await wait_for_audio_chunks(
            conversation_id=conversation_id, max_wait_seconds=30, min_chunks=1
        )

        if not chunks_ready:
            await mark_conversation_deleted(
                conversation_id=conversation_id,
                deletion_reason="audio_chunks_not_ready",
            )
            end_of_conversation_handled = True
            return await handle_end_of_conversation(
                session_id=session_id,
                conversation_id=conversation_id,
                client_id=client_id,
                user_id=user_id,
                start_time=state.start_time,
                last_result_count=state.last_result_count,
                timeout_triggered=state.timeout_triggered,
                redis_client=redis_client,
                end_reason=state.end_reason,
            )

        logger.info(
            f"📦 MongoDB audio chunks ready for conversation {conversation_id[:12]}"
        )

        # Phase 6: Save streaming transcript
        version_id = await _save_streaming_transcript(
            session_id=session_id,
            conversation_id=conversation_id,
            aggregator=aggregator,
        )

        # Phase 7: Enqueue post-processing pipeline
        await _enqueue_post_processing(
            conversation_id=conversation_id,
            user_id=user_id,
            client_id=client_id,
            version_id=version_id,
            end_reason=state.end_reason,
        )

        # Cleanup and session restart
        end_of_conversation_handled = True
        return await handle_end_of_conversation(
            session_id=session_id,
            conversation_id=conversation_id,
            client_id=client_id,
            user_id=user_id,
            start_time=state.start_time,
            last_result_count=state.last_result_count,
            timeout_triggered=state.timeout_triggered,
            redis_client=redis_client,
            end_reason=state.end_reason,
        )
    finally:
        if not end_of_conversation_handled:
            logger.error(
                f"⚠️ open_conversation_job post-processing failed for {conversation_id}, "
                f"performing emergency cleanup to re-enable speech detection"
            )
            try:
                await handle_end_of_conversation(
                    session_id=session_id,
                    conversation_id=conversation_id,
                    client_id=client_id,
                    user_id=user_id,
                    start_time=state.start_time,
                    last_result_count=state.last_result_count,
                    timeout_triggered=state.timeout_triggered,
                    redis_client=redis_client,
                    end_reason="error",
                )
            except Exception as cleanup_error:
                logger.error(f"❌ Emergency cleanup also failed: {cleanup_error}")


@async_job(redis=True, beanie=True)
async def generate_title_summary_job(
    conversation_id: str, *, redis_client=None
) -> Dict[str, Any]:
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
        generate_detailed_summary,
        generate_title_and_summary,
    )

    set_otel_session(conversation_id)
    logger.info(
        f"📝 Starting title/summary generation for conversation {conversation_id}"
    )

    start_time = time.time()

    # Get the conversation
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    # Get transcript and segments (properties return data from active transcript version)
    transcript_text = conversation.transcript or ""
    segments = conversation.segments or []

    if not transcript_text and (not segments or len(segments) == 0):
        logger.warning(
            f"⚠️ No transcript or segments available for conversation {conversation_id}"
        )
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

        # Fetch memory context for richer detailed summaries
        # Use the entire transcript as the search query for best semantic matching
        # so all key topics/entities in the conversation can find relevant memories
        memory_context = None
        try:
            from advanced_omi_backend.services.memory import get_memory_service

            memory_service = get_memory_service()
            memories = await memory_service.search_memories(
                transcript_text, conversation.user_id, limit=10
            )
            if memories:
                memory_context = "\n".join(m.content for m in memories if m.content)
                logger.info(
                    f"📚 Retrieved {len(memories)} memories as context for detailed summary"
                )
            else:
                logger.info(f"📚 No memories found for context enrichment")
        except Exception as mem_error:
            logger.warning(
                f"⚠️ Could not fetch memory context (continuing without): {mem_error}"
            )

        # Generate title+summary (one call) and detailed summary in parallel
        import asyncio

        (title, short_summary), detailed_summary = await asyncio.gather(
            generate_title_and_summary(
                transcript_text,
                segments=segments,
                user_id=conversation.user_id,
            ),
            generate_detailed_summary(
                transcript_text,
                segments=segments,
                memory_context=memory_context,
            ),
        )

        conversation.title = title
        conversation.summary = short_summary
        conversation.detailed_summary = detailed_summary

        logger.info(f"✅ Generated title: '{conversation.title}'")
        logger.info(f"✅ Generated summary: '{conversation.summary}'")
        logger.info(
            f"✅ Generated detailed summary: {len(conversation.detailed_summary)} chars"
        )

        # Update processing status for placeholder/reprocessing conversations
        if getattr(conversation, "processing_status", None) in [
            "pending_transcription",
            "reprocessing",
        ]:
            conversation.processing_status = "completed"
            logger.info(
                f"✅ Updated placeholder conversation {conversation_id} "
                f"processing_status to 'completed'"
            )

    except Exception as gen_error:
        logger.error(f"❌ Title/summary generation failed: {gen_error}")

        # Mark placeholder/reprocessing conversation as failed
        if getattr(conversation, "processing_status", None) in [
            "pending_transcription",
            "reprocessing",
        ]:
            conversation.title = "Audio Recording (Transcription Failed)"
            conversation.summary = f"Title/summary generation failed: {str(gen_error)}"
            conversation.processing_status = "transcription_failed"
            await conversation.save()
            logger.warning(
                f"⚠️ Marked placeholder conversation {conversation_id} "
                f"as transcription_failed (title/summary generation error). Audio is still saved."
            )

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
    update_job_meta(
        conversation_id=conversation_id,
        title=conversation.title,
        summary=conversation.summary,
        detailed_summary_length=(
            len(conversation.detailed_summary) if conversation.detailed_summary else 0
        ),
        segment_count=len(segments),
        processing_time=processing_time,
    )

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
    end_reason: Optional[str] = None,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    Dispatch conversation.complete plugin event for all conversation sources.

    This job runs at the end of conversation processing to ensure plugins
    receive the conversation.complete event with the correct end_reason.
    Used by both file upload and WebSocket streaming paths.

    Args:
        conversation_id: Conversation ID
        client_id: Client ID
        user_id: User ID
        end_reason: Reason the conversation ended (e.g., 'file_upload', 'websocket_disconnect', 'user_stopped')
                   Defaults to 'file_upload' for backward compatibility
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with success status and plugin results
    """
    from advanced_omi_backend.models.conversation import Conversation

    logger.info(
        f"📌 Dispatching conversation.complete event for conversation {conversation_id}"
    )

    start_time = time.time()

    # Get the conversation to include in event data
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    # Save end_reason and completed_at to database if not already set
    # This ensures end_reason is persisted before plugins receive conversation.complete event
    if end_reason and conversation.end_reason is None:
        try:
            conversation.end_reason = Conversation.EndReason(end_reason)
        except ValueError:
            logger.warning(f"⚠️ Invalid end_reason '{end_reason}', using UNKNOWN")
            conversation.end_reason = Conversation.EndReason.UNKNOWN

        if conversation.completed_at is None:
            conversation.completed_at = datetime.utcnow()

        await conversation.save()
        logger.info(
            f"💾 Saved end_reason={conversation.end_reason} to conversation {conversation_id[:12]} in event dispatch job"
        )

    # Get user email for event data
    from advanced_omi_backend.models.user import User

    user = await User.get(user_id)
    user_email = user.email if user else ""

    # Prepare plugin event data (same format as open_conversation_job)
    actual_end_reason = end_reason or "file_upload"
    try:
        plugin_results = await dispatch_plugin_event(
            event=PluginEvent.CONVERSATION_COMPLETE,
            user_id=user_id,
            data={
                "conversation": {
                    "client_id": client_id,
                    "user_id": user_id,
                },
                "transcript": conversation.transcript if conversation else "",
                "duration": 0,  # Duration not tracked for file uploads
                "conversation_id": conversation_id,
            },
            metadata={"end_reason": actual_end_reason},
            description=f"conversation={conversation_id[:12]}, end_reason={actual_end_reason}",
            require_router=True,
        )

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

    except RuntimeError as e:
        logger.error(f"❌ {e}")
        return {
            "success": False,
            "skipped": True,
            "reason": "No plugin router",
            "conversation_id": conversation_id,
            "error": str(e),
        }
    except Exception as e:
        logger.warning(f"⚠️ Error dispatching conversation complete event: {e}")
        return {
            "success": False,
            "error": str(e),
            "conversation_id": conversation_id,
        }
