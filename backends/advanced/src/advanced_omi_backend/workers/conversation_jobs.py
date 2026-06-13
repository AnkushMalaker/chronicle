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
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from rq.exceptions import NoSuchJobError
from rq.job import Job

from advanced_omi_backend.config import get_live_segmentation
from advanced_omi_backend.controllers.queue_controller import (
    redis_conn,
    start_post_conversation_jobs,
)
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.observability.otel_setup import (
    set_otel_session,
    set_span_attrs,
    set_trace_io,
    traced_job,
)
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
)
from advanced_omi_backend.services.plugin_service import (
    dispatch_plugin_event,
    get_plugin_router,
)
from advanced_omi_backend.services.sse_publisher import (
    publish_sse_event,
    publish_sse_event_throttled,
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
    store = SessionStore(redis_client)
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

        # NOTE: session-hash TTL is handled below, gated on whether the session is
        # ending vs. continuing. Setting it here (unconditionally) would let the hash
        # expire mid-session for a still-active connection → zombie session.
    except Exception as cleanup_error:
        logger.warning(f"⚠️ Error during stream cleanup: {cleanup_error}")

    # Delete the conversation:current signal so audio persistence knows conversation ended.
    # May already be deleted by open_conversation_job for close_requested/timeout cases
    # (early delete to stop audio persistence writing to the closed conversation).
    # Redis DEL on a non-existent key is a no-op.
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

        conversation.completed_at = datetime.now(timezone.utc)
        await conversation.save()
        logger.info(
            f"💾 Saved conversation {conversation_id[:12]} end_reason: {conversation.end_reason}"
        )
    else:
        logger.warning(
            f"⚠️ Conversation {conversation_id} not found for end reason tracking"
        )

    # Increment conversation count for this session
    conversation_count = await store.increment_conversation_count(session_id)
    logger.info(f"📊 Conversation count for session {session_id}: {conversation_count}")

    # Check if session is still active (user still recording) and restart listening jobs
    # Fetch status, websocket_connected, and completion_reason in one Redis call
    status, ws_connected, completion_reason = await store.get_status_ws_reason(
        session_id
    )

    if status is not None:
        # Determine if we should restart speech detection
        # Only restart when session is explicitly active.
        # status=finalizing means the session is ending (audio-stop or disconnect),
        # so re-enqueueing speech detection for the same session is always wrong.
        should_restart = False
        if status == SessionStatus.ACTIVE:
            should_restart = True
        else:
            logger.info(
                f"Session {session_id[:12]}: status={status.value}, "
                f"ws_connected={ws_connected}, completion_reason={completion_reason} "
                f"— not restarting speech detection."
            )

        if should_restart:
            # Session still active - enqueue new speech detection for next conversation.
            # Clear any TTL: the hash must live as long as the connection does, otherwise
            # a quiet gap > TTL expires it mid-session → next close reads status=None →
            # speech detection never restarts (deaf connection). See Docs/system-oddities.md #4.
            await store.persist_session(session_id)
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
            # Session ending (finalizing/finished): set a backstop TTL so the hash
            # self-cleans if the disconnect path didn't already remove it.
            await store.expire_session(session_id, 3600)
            logger.info(
                f"Session {session_id} status={status.value}, ws_connected={ws_connected}, "
                f"not restarting (user stopped recording) — set 1h cleanup TTL"
            )
    else:
        logger.info(f"Session {session_id} not found, not restarting (session ended)")

    # Notify frontend that conversation has closed and post-processing will start
    publish_sse_event(
        user_id,
        "conversation.closed",
        {
            "conversation_id": conversation_id,
            "client_id": client_id,
            "end_reason": end_reason,
        },
    )

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
    live_version_created: bool = False
    last_live_write_time: float = 0.0


@dataclass
class LiveTranscriptState:
    """Incremental accumulation of transcription results.

    Instead of re-reading the entire Redis Stream via XRANGE every second,
    this tracks position in the stream and accumulates only new results.
    """

    text_parts: list
    all_words: list
    all_segments: list
    chunk_count: int = 0
    total_confidence: float = 0.0
    provider: Optional[str] = None
    last_stream_id: str = "0"

    def add_results(self, new_results: list) -> None:
        """Incrementally add new transcription results."""
        for result in new_results:
            text = result.get("text", "").strip()
            if text:
                self.text_parts.append(text)
            self.all_words.extend(result.get("words", []))
            self.all_segments.extend(result.get("segments", []))
            self.total_confidence += result.get("confidence", 0.0)
            self.chunk_count += 1
            if self.provider is None:
                self.provider = result.get("provider")

    def to_combined(self) -> dict:
        """Return dict matching ``get_combined_results()`` format."""
        return {
            "text": " ".join(self.text_parts),
            "words": self.all_words,
            "segments": self.all_segments,
            "chunk_count": self.chunk_count,
            "total_confidence": (
                self.total_confidence / self.chunk_count
                if self.chunk_count > 0
                else 0.0
            ),
            "provider": self.provider,
            "word_count": len(self.all_words),
        }


async def _wait_for_new_results(
    redis_client, session_id: str, last_stream_id: str
) -> tuple:
    """Block until new transcription results arrive on the Redis Stream.

    Uses XREAD with blocking to avoid polling. Returns only NEW messages
    since ``last_stream_id``.

    Returns:
        (parsed_results_list, new_last_id)
    """
    stream_name = f"transcription:results:{session_id}"
    try:
        messages = await redis_client.xread(
            {stream_name: last_stream_id}, count=100, block=10000
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Error in _wait_for_new_results: {e}")
        return [], last_stream_id

    if not messages:
        return [], last_stream_id

    results = []
    new_last_id = last_stream_id
    for _stream_key, msgs in messages:
        for message_id, fields in msgs:
            mid = message_id if isinstance(message_id, str) else message_id.decode()
            result = {
                "message_id": mid,
                "text": (fields[b"text"].decode() if b"text" in fields else ""),
                "confidence": float(
                    fields.get(b"confidence", b"0.0").decode()
                    if isinstance(fields.get(b"confidence", b"0.0"), bytes)
                    else fields.get(b"confidence", 0.0)
                ),
                "provider": (
                    fields[b"provider"].decode() if b"provider" in fields else "unknown"
                ),
                "chunk_id": (
                    fields.get(b"chunk_id", b"unknown").decode()
                    if isinstance(fields.get(b"chunk_id", b"unknown"), bytes)
                    else str(fields.get(b"chunk_id", "unknown"))
                ),
                "timestamp": float(
                    fields.get(b"timestamp", b"0.0").decode()
                    if isinstance(fields.get(b"timestamp", b"0.0"), bytes)
                    else fields.get(b"timestamp", 0.0)
                ),
            }
            if b"words" in fields:
                result["words"] = json.loads(
                    fields[b"words"].decode()
                    if isinstance(fields[b"words"], bytes)
                    else fields[b"words"]
                )
            if b"segments" in fields:
                result["segments"] = json.loads(
                    fields[b"segments"].decode()
                    if isinstance(fields[b"segments"], bytes)
                    else fields[b"segments"]
                )
            results.append(result)
            new_last_id = mid

    return results, new_last_id


async def _wait_for_signal(pubsub) -> Optional[dict]:
    """Block until a session signal arrives via Redis Pub/Sub.

    Returns parsed JSON signal dict, or None on timeout.
    """
    try:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=10.0)
        if message and message["type"] == "message":
            data = message["data"]
            if isinstance(data, bytes):
                data = data.decode()
            return json.loads(data)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Error in _wait_for_signal: {e}")
    return None


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
        publish_sse_event(
            user_id,
            "conversation.created",
            {
                "conversation_id": conversation_id,
                "client_id": client_id,
                "title": "Recording...",
            },
        )

    # Attach markers from Redis session (e.g., button events captured during streaming)
    markers = await SessionStore(redis_client).get_markers(session_id)
    if markers:
        conversation.markers = markers
        await conversation.save()
        logger.info(
            f"📌 Attached {len(conversation.markers)} markers to conversation {conversation_id}"
        )

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
    """Monitor transcription results and session signals until conversation ends.

    Event-driven architecture using ``asyncio.wait()`` with:
    - Redis ``XREAD`` blocking for new transcription results (no full-stream re-read)
    - Redis Pub/Sub for session signals (finalize, close_requested)
    - Periodic timeout for housekeeping (zombie detection, inactivity check)

    Mutates ``state`` in place with final values for timeout_triggered,
    close_requested_reason, last_result_count, and last_word_count.
    """
    from advanced_omi_backend.utils.job_utils import check_job_alive

    store = SessionStore(redis_client)
    max_runtime = 86400  # 24h safety ceiling

    # Inactivity timeout configuration
    inactivity_timeout_seconds = float(
        os.getenv("SPEECH_INACTIVITY_THRESHOLD_SECONDS", "60")
    )
    inactivity_timeout_minutes = inactivity_timeout_seconds / 60
    last_inactivity_log_time = time.time()

    # Audio-stream idle watchdog (wall-clock). The producer bumps last_chunk_at on
    # every audio chunk, so it advances continuously while the device streams — even
    # during silence — and freezes the instant the device disconnects. The
    # audio-timestamp-based inactivity check below cannot grow without new results,
    # so on a sudden disconnect it would hang until the 24h ceiling. This watchdog
    # is the reliable "device stopped streaming" signal, independent of WebSocket
    # cleanup running. It only applies when live transcription is enabled — in
    # "off"/batch mode no streaming worker fills the aggregator and chunks may stop
    # without the conversation being stuck (the batch path closes it).
    expects_live_results = get_live_segmentation() != "off"
    stream_idle_timeout_seconds = float(os.getenv("STREAM_IDLE_TIMEOUT_SECONDS", "30"))
    health_check_interval = 5.0  # wall-clock cadence for status + chunk-idle polling
    last_health_check_time = time.time()

    # Test mode: wait for audio queue to drain before timing out
    wait_for_queue_drain = (
        os.getenv("WAIT_FOR_AUDIO_QUEUE_DRAIN", "false").lower() == "true"
    )

    logger.info(
        f"📊 Conversation timeout configured: {inactivity_timeout_minutes} minutes ({inactivity_timeout_seconds}s)"
    )
    if wait_for_queue_drain:
        logger.info("🧪 Test mode: Waiting for audio queue to drain before timeout")

    # Subscribe to session signals via Pub/Sub
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"session:signal:{state.session_id}")

    # Incremental result accumulation (replaces full XRANGE every second)
    transcript_state = LiveTranscriptState(text_parts=[], all_words=[], all_segments=[])

    # Track inactivity based on accumulated speech data
    inactivity_duration = 0.0

    try:
        while True:
            # Create concurrent event sources
            results_task = asyncio.create_task(
                _wait_for_new_results(
                    redis_client,
                    state.session_id,
                    transcript_state.last_stream_id,
                )
            )
            signal_task = asyncio.create_task(_wait_for_signal(pubsub))

            # Wait for EITHER new results, a signal, or timeout (housekeeping)
            done, pending = await asyncio.wait(
                {results_task, signal_task},
                timeout=10.0,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            # --- Handle signal ---
            if signal_task in done:
                signal = signal_task.result()
                if signal:
                    sig_type = signal.get("type")
                    sig_reason = signal.get("reason", "unknown")

                    if sig_type == "finalize":
                        if sig_reason == "websocket_disconnect":
                            logger.warning(
                                f"🔌 WebSocket disconnected for session {state.session_id[:12]} - "
                                f"ending conversation early"
                            )
                        else:
                            logger.info(f"🛑 Session finalizing (reason: {sig_reason})")
                        break

                    elif sig_type == "close_requested":
                        await store.take_close_request(state.session_id)
                        state.close_requested_reason = sig_reason
                        logger.info(f"🔒 Conversation close requested: {sig_reason}")
                        state.timeout_triggered = True
                        break

                    elif sig_type == "session_complete":
                        # Check for spurious "finished" from status endpoint race
                        if sig_reason == "all_jobs_complete":
                            if await store.is_websocket_connected(state.session_id):
                                logger.warning(
                                    f"⚠️ Ignoring spurious 'finished' for session {state.session_id[:12]}: "
                                    f"websocket_connected=true, reason=all_jobs_complete. "
                                    f"Resetting status to 'active'."
                                )
                                await store.set_status_active(state.session_id)
                                # Continue monitoring
                            else:
                                break
                        else:
                            break

            # --- Handle new transcription results ---
            if results_task in done:
                new_results, new_last_id = results_task.result()
                if new_results:
                    transcript_state.add_results(new_results)
                    transcript_state.last_stream_id = new_last_id
                    combined = transcript_state.to_combined()
                    current_count = combined["chunk_count"]

                    # Analyze speech content
                    transcript_data = {
                        "text": combined["text"],
                        "words": combined.get("words", []),
                    }
                    speech_analysis = analyze_speech(transcript_data)

                    # Validate segments and extract speakers
                    validated_segments = _validate_segments(
                        combined.get("segments", [])
                    )
                    speakers = extract_speakers_from_segments(validated_segments)

                    # Track speech activity
                    new_speech_time, state.last_word_count = (
                        await track_speech_activity(
                            speech_analysis=speech_analysis,
                            last_word_count=state.last_word_count,
                            conversation_id=state.conversation_id,
                            redis_client=redis_client,
                        )
                    )
                    if new_speech_time:
                        state.last_meaningful_speech_time = new_speech_time

                    # Update job metadata
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

                    # Push live progress via SSE
                    publish_sse_event_throttled(
                        state.user_id,
                        "job.progress",
                        {
                            "conversation_id": state.conversation_id,
                            "job_type": "open_conversation_job",
                            "word_count": combined.get("word_count", 0),
                            "duration_seconds": speech_analysis.get("duration", 0),
                            "speakers": speakers,
                            "has_speech": speech_analysis.get("has_speech", False),
                        },
                    )

                    # Update inactivity tracking
                    current_audio_time = speech_analysis.get("speech_end", 0.0)
                    if current_audio_time > 0 and state.last_meaningful_speech_time > 0:
                        inactivity_duration = (
                            current_audio_time - state.last_meaningful_speech_time
                        )
                    else:
                        inactivity_duration = 0

                    # Track results progress
                    if current_count > state.last_result_count:
                        logger.info(
                            f"📊 Conversation {state.conversation_id} progress: "
                            f"{current_count} results, {len(combined['text'])} chars, "
                            f"{len(validated_segments)} segments"
                        )
                        state.last_result_count = current_count

                        # Update live transcript in MongoDB (throttled to every 5s)
                        try:
                            now_live = time.time()
                            if not state.live_version_created:
                                provider = combined.get("provider", "deepgram")
                                await _create_live_transcript_version(
                                    conversation_id=state.conversation_id,
                                    combined=combined,
                                    validated_segments=validated_segments,
                                    provider=provider,
                                )
                                state.live_version_created = True
                                state.last_live_write_time = now_live
                                publish_sse_event(
                                    state.user_id,
                                    "transcript.live",
                                    {
                                        "conversation_id": state.conversation_id,
                                        "segments": validated_segments,
                                        "transcript": combined.get("text", ""),
                                        "word_count": combined.get("word_count", 0),
                                    },
                                )
                            elif now_live - state.last_live_write_time >= 5.0:
                                await _update_live_transcript(
                                    conversation_id=state.conversation_id,
                                    combined=combined,
                                    validated_segments=validated_segments,
                                )
                                state.last_live_write_time = now_live
                                publish_sse_event(
                                    state.user_id,
                                    "transcript.live",
                                    {
                                        "conversation_id": state.conversation_id,
                                        "segments": validated_segments,
                                        "transcript": combined.get("text", ""),
                                        "word_count": combined.get("word_count", 0),
                                    },
                                )
                        except Exception as e:
                            logger.warning(f"⚠️ Error updating live transcript: {e}")

                        # Dispatch transcript.streaming plugin events
                        try:
                            plugin_router = get_plugin_router()
                            if plugin_router:
                                transcript_text = combined.get("text", "")
                                if transcript_text:
                                    plugin_data = {
                                        "transcript": transcript_text,
                                        "segment_id": f"{state.session_id}_{current_count}",
                                        "conversation_id": state.conversation_id,
                                        "segments": validated_segments,
                                        "word_count": speech_analysis.get(
                                            "word_count", 0
                                        ),
                                    }
                                    logger.info(
                                        f"🔌 DISPATCH: transcript.streaming event "
                                        f"(conversation={state.conversation_id[:12]}, "
                                        f"segment_id={state.session_id}_{current_count})"
                                    )
                                    plugin_results = await plugin_router.dispatch_event(
                                        event=PluginEvent.TRANSCRIPT_STREAMING,
                                        user_id=state.user_id,
                                        data=plugin_data,
                                        metadata={"client_id": state.client_id},
                                    )
                                    logger.info(
                                        f"🔌 RESULT: transcript.streaming dispatched to "
                                        f"{len(plugin_results) if plugin_results else 0} plugins"
                                    )
                                    if plugin_results:
                                        for result in plugin_results:
                                            if result.message:
                                                logger.info(
                                                    f"  Plugin: {result.message}"
                                                )
                                            if not result.should_continue:
                                                logger.info(
                                                    f"  Plugin stopped normal processing"
                                                )
                        except Exception as e:
                            logger.warning(
                                f"⚠️ Error triggering transcript-level plugins: {e}"
                            )

            # --- Housekeeping (runs on timeout or after processing) ---
            current_time = time.time()

            # Zombie detection
            if not await check_job_alive(redis_client, current_job, state.session_id):
                break

            # Max runtime
            if current_time - state.start_time > max_runtime:
                logger.warning(f"⏱️ Max runtime reached for {state.conversation_id}")
                break

            # Periodic health check (status + audio-stream liveness). Runs on a
            # wall-clock cadence regardless of whether results/signals fired this
            # iteration — so a missed pub/sub finalize signal or a silently
            # disconnected device is still detected, not just when the loop happens
            # to time out idle.
            if current_time - last_health_check_time >= health_check_interval:
                last_health_check_time = current_time

                status, ws_connected, reason_str = await store.get_status_ws_reason(
                    state.session_id
                )
                reason_str = reason_str or "unknown"
                if status in (SessionStatus.FINALIZING, SessionStatus.FINISHED):
                    if (
                        status == SessionStatus.FINISHED
                        and ws_connected
                        and reason_str == "all_jobs_complete"
                    ):
                        await store.set_status_active(state.session_id)
                    else:
                        logger.info(
                            f"🔍 Health check: session {state.session_id[:12]} "
                            f"status={status.value}, reason={reason_str}"
                        )
                        if reason_str == "websocket_disconnect":
                            state.timeout_triggered = False
                        break

                # Catch a missed close_requested signal
                close_reason = await store.take_close_request(state.session_id)
                if close_reason:
                    state.close_requested_reason = close_reason
                    logger.info(
                        f"🔍 Health check: conversation close requested: {state.close_requested_reason}"
                    )
                    state.timeout_triggered = True
                    break

                # Audio-stream idle watchdog: no audio chunks for too long means the
                # device stopped streaming (sudden disconnect) even though status may
                # still read ACTIVE (WebSocket cleanup not yet run). Skipped in
                # "off"/batch mode, where no streaming worker produces chunks here.
                if expects_live_results:
                    last_chunk_at = await store.get_last_chunk_at(state.session_id)
                    if (
                        last_chunk_at
                        and current_time - last_chunk_at > stream_idle_timeout_seconds
                    ):
                        logger.warning(
                            f"🔌 No audio chunks for {current_time - last_chunk_at:.0f}s "
                            f"(threshold {stream_idle_timeout_seconds:.0f}s) — device "
                            f"stopped streaming, closing conversation "
                            f"{state.conversation_id[:12]}"
                        )
                        state.timeout_triggered = True
                        break

            # Inactivity log and check
            if current_time - last_inactivity_log_time >= 10:
                logger.info(
                    f"⏱️ Time since last speech: {inactivity_duration:.1f}s "
                    f"(timeout: {inactivity_timeout_seconds:.0f}s)"
                )
                last_inactivity_log_time = current_time

            if inactivity_duration > inactivity_timeout_seconds:
                if wait_for_queue_drain:
                    persist_queue_key = f"audio:queue:{state.session_id}"
                    queue_length = await redis_client.llen(persist_queue_key)
                    if queue_length > 0:
                        logger.info(
                            f"🧪 Test mode: Inactivity timeout reached but "
                            f"{queue_length} chunks still in queue, waiting..."
                        )
                        continue

                logger.info(
                    f"🕐 Conversation {state.conversation_id} inactive for "
                    f"{inactivity_duration / 60:.1f} minutes "
                    f"(threshold: {inactivity_timeout_minutes} min), "
                    f"auto-closing conversation..."
                )
                state.timeout_triggered = True
                break

    finally:
        await pubsub.unsubscribe(f"session:signal:{state.session_id}")
        await pubsub.aclose()


async def _create_live_transcript_version(
    conversation_id: str,
    combined: dict,
    validated_segments: list,
    provider: str,
) -> None:
    """Create the initial live-v0 transcript version via Beanie update.

    Uses a single atomic update to push the version and set it as active,
    avoiding a full Beanie document load/save cycle.
    """
    from advanced_omi_backend.models.conversation import Conversation

    transcript_text = combined.get("text", "")
    segments_as_dicts = [
        {
            "start": s.get("start", 0.0),
            "end": s.get("end", 0.0),
            "text": s.get("text", ""),
            "speaker": str(s.get("speaker", "SPEAKER_00")),
            "segment_type": s.get("segment_type", "speech"),
            "identified_as": s.get("identified_as"),
            "confidence": s.get("confidence"),
            "words": [],
        }
        for s in validated_segments
    ]

    version_doc = {
        "version_id": "live-v0",
        "transcript": transcript_text,
        "words": [],
        "segments": segments_as_dicts,
        "provider": provider,
        "model": provider,
        "created_at": datetime.now(timezone.utc),
        "processing_time_seconds": None,
        "diarization_source": "provider" if segments_as_dicts else None,
        "metadata": {
            "source": "live_streaming",
            "word_count": combined.get("word_count", 0),
        },
    }

    result = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    ).update(
        {
            "$push": {"transcript_versions": version_doc},
            "$set": {"active_transcript_version": "live-v0"},
        }
    )

    logger.info(
        f"📡 Created live-v0 transcript version for {conversation_id[:12]} "
        f"({len(transcript_text)} chars, {len(segments_as_dicts)} segments)"
    )


async def _update_live_transcript(
    conversation_id: str,
    combined: dict,
    validated_segments: list,
) -> None:
    """Update the live-v0 transcript version in-place via positional $ operator.

    Efficient partial update — only touches the transcript text, segments, and
    metadata.word_count within the matching array element.
    Uses PyMongo collection (sync) wrapped in Beanie's find pattern.
    """
    from advanced_omi_backend.models.conversation import Conversation

    transcript_text = combined.get("text", "")
    segments_as_dicts = [
        {
            "start": s.get("start", 0.0),
            "end": s.get("end", 0.0),
            "text": s.get("text", ""),
            "speaker": str(s.get("speaker", "SPEAKER_00")),
            "segment_type": s.get("segment_type", "speech"),
            "identified_as": s.get("identified_as"),
            "confidence": s.get("confidence"),
            "words": [],
        }
        for s in validated_segments
    ]

    # Use PyMongo collection for positional $ operator (Beanie doesn't support it)
    collection = Conversation.get_pymongo_collection()
    collection.update_one(
        {
            "conversation_id": conversation_id,
            "transcript_versions.version_id": "live-v0",
        },
        {
            "$set": {
                "transcript_versions.$.transcript": transcript_text,
                "transcript_versions.$.segments": segments_as_dicts,
                "transcript_versions.$.metadata.word_count": combined.get(
                    "word_count", 0
                ),
            }
        },
    )


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

    # Remove live-v0 version if it exists (replaced by final streaming version)
    live_removed = False
    conversation.transcript_versions = [
        v for v in conversation.transcript_versions if v.version_id != "live-v0"
    ]
    if conversation.active_transcript_version == "live-v0":
        conversation.active_transcript_version = None
        live_removed = True
    if live_removed:
        logger.info(
            f"🔄 Removed live-v0 transcript version, replacing with final streaming version"
        )

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

    # Use provider-supplied segments if available (from streaming diarization).
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
        provider_diarized = True
    else:
        # Non-diarizing streaming provider returned no segments. Build a single
        # fallback segment from the transcript text/words so the transcript is always
        # renderable in the UI (the frontend only shows the segment list). This mirrors
        # the batch path's fallback. If speaker recognition is enabled, the
        # post-conversation speaker job replaces this with diarized segments.
        provider_diarized = False
        if transcript_text:
            start_time_audio = words_data[0].get("start", 0.0) if words_data else 0.0
            end_time_audio = words_data[-1].get("end", 0.0) if words_data else 0.0
            segments = [
                Conversation.SpeakerSegment(
                    speaker="Speaker 0",
                    start=start_time_audio,
                    end=end_time_audio,
                    text=transcript_text,
                    words=words,
                )
            ]
        else:
            segments = []

    # Determine provider from streaming results
    provider = final_transcript.get("provider", "deepgram")

    # Diarization source reflects real provider diarization, not the fallback segment
    diarization_source = "provider" if provider_diarized else None

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
            "provider_capabilities": {"diarization": provider_diarized},
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
    if provider_diarized:
        segment_info = f"{len(segments)} provider segments (diarization_source={diarization_source})"
    elif segments:
        segment_info = f"{len(segments)} fallback segment (pending speaker recognition)"
    else:
        segment_info = "0 segments (empty transcript)"
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
            job_timeout=-1,
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
    speech_job_id: Optional[str] = None,
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

    # When session stays active (timeout or close_requested), immediately clear
    # conversation:current so audio persistence stops writing to this conversation.
    # Without this, audio persistence keeps adding chunks during phases 4-7
    # (potentially 30+ seconds), corrupting the closed conversation's data and
    # delaying the start of the next speech detection cycle.
    # For session finalization (disconnect), audio persistence exits on its own
    # via the session status check, so this is not needed.
    if state.timeout_triggered:
        current_conversation_key = f"conversation:current:{session_id}"
        await redis_client.delete(current_conversation_key)
        logger.info(
            f"🔄 Cleared conversation:current for {conversation_id[:12]} — "
            f"audio persistence will flush buffer and wait for next conversation"
        )

    logger.info(
        f"✅ Conversation {conversation_id} updates complete, checking for meaningful speech..."
    )

    # Phase 3: Determine end reason
    completion_reason_str = await SessionStore(redis_client).get_completion_reason(
        session_id
    )

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
    elif time.time() - state.start_time > 86400:
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
@traced_job("title_summary", pipeline_stage="title_summary", gen_ai_operation="chat")
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

    set_span_attrs(user_id=str(conversation.user_id))

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

    set_trace_io(input={"transcript": transcript_text})

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

    publish_sse_event(
        str(conversation.user_id),
        "conversation.updated",
        {
            "conversation_id": conversation_id,
            "title": conversation.title,
            "summary": conversation.summary,
        },
    )

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

    result = {
        "success": True,
        "conversation_id": conversation_id,
        "title": conversation.title,
        "summary": conversation.summary,
        "detailed_summary": conversation.detailed_summary,
        "processing_time_seconds": processing_time,
    }
    set_trace_io(output=result)
    return result


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
            conversation.completed_at = datetime.now(timezone.utc)

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

        publish_sse_event(
            user_id,
            "conversation.completed",
            {
                "conversation_id": conversation_id,
                "end_reason": actual_end_reason,
            },
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
