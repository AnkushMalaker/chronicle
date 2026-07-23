"""
Session controller for handling audio session-related business logic.

This module manages Redis-based audio streaming sessions, including:
- Session metadata and status
- Conversation counts per session
- Session lifecycle tracking
"""

import logging
import time

from fastapi.responses import JSONResponse

from advanced_omi_backend.controllers.queue_controller import (
    all_jobs_complete_for_client,
    default_queue,
    memory_queue,
    transcription_queue,
)
from advanced_omi_backend.services.audio_stream.durability import (
    AUDIO_PERSISTENCE_GROUP,
    delete_stream_if_durable,
)
from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
    SessionView,
)

logger = logging.getLogger(__name__)


def _session_info_dict(view: SessionView, conversation_count: int) -> dict:
    """Shape a SessionView into the session-info response dict used by the API."""
    now = time.time()
    return {
        "session_id": view.session_id,
        "user_id": view.user_id,
        "client_id": view.client_id,
        "provider": view.provider,
        "mode": view.mode,
        "status": view.status.value if view.status else "",
        "websocket_connected": view.websocket_connected,
        "completion_reason": view.completion_reason,
        "chunks_published": view.chunks_published,
        "started_at": view.started_at,
        "last_chunk_at": view.last_chunk_at,
        "age_seconds": now - view.started_at,
        "idle_seconds": now - view.last_chunk_at,
        "conversation_count": conversation_count,
        # Speech detection events
        "last_event": view.last_event,
        "speech_detected_at": view.speech_detected_at,
        "speaker_check_status": (
            view.speaker_check_status.value if view.speaker_check_status else ""
        ),
        "identified_speakers": ",".join(view.identified_speakers),
    }


async def get_streaming_status(request):
    """Get status of active streaming sessions and Redis Streams health."""
    try:
        # Get Redis client from request.app.state (initialized during startup)
        redis_client = request.app.state.redis_audio_stream

        if not redis_client:
            return JSONResponse(
                status_code=503,
                content={"error": "Redis client for audio streaming not initialized"},
            )

        # Get all sessions (both active and completed)
        store = SessionStore(redis_client)
        active_sessions = []
        completed_sessions_from_redis = []

        async for view in store.iter_views():
            conversation_count = await store.get_conversation_count(view.session_id)
            session_obj = _session_info_dict(view, conversation_count)

            # Separate active and completed sessions
            # Check if all jobs are complete (including failed jobs)
            all_jobs_done = all_jobs_complete_for_client(view.client_id)

            # Session is completed ONLY when:
            # 1. Status was already set to "finished" by an authoritative source
            #    (WebSocket disconnect handler or job handler), AND
            # 2. All RQ jobs are in terminal state
            #
            # IMPORTANT: Do NOT mark sessions as finished here. Between conversations
            # (after open_conversation_job finishes, before speech detection restarts),
            # all jobs are briefly terminal. Writing "finished" during this gap kills
            # the session permanently.
            if view.status == SessionStatus.FINISHED and all_jobs_done:
                completed_sessions_from_redis.append(
                    {
                        "session_id": view.session_id,
                        "client_id": view.client_id,
                        "completed_at": view.completed_at or view.last_chunk_at,
                        "conversation_count": conversation_count,
                    }
                )
            else:
                # Active session (including inter-conversation gaps where all jobs
                # are temporarily terminal but status is still "active")
                active_sessions.append(session_obj)

        # Get stream health for all session-scoped streams.
        # Categorize as active or completed based on consumer activity
        active_streams = {}
        completed_streams = {}

        # Create a map of session_id to session for quick lookup.
        session_by_id = {}
        for session in active_sessions + completed_sessions_from_redis:
            session_id = session.get("session_id")
            if session_id:
                session_by_id[session_id] = session

        # Discover all audio streams
        stream_keys = await redis_client.keys("audio:stream:*")
        current_time = time.time()

        for stream_key in stream_keys:
            stream_name = (
                stream_key.decode() if isinstance(stream_key, bytes) else stream_key
            )
            try:
                # Check if stream exists
                stream_info = await redis_client.execute_command(
                    "XINFO", "STREAM", stream_name
                )

                # Parse stream info (returns flat list of key-value pairs)
                info_dict = {}
                for i in range(0, len(stream_info), 2):
                    key = (
                        stream_info[i].decode()
                        if isinstance(stream_info[i], bytes)
                        else str(stream_info[i])
                    )
                    value = stream_info[i + 1]

                    # Skip complex binary structures like first-entry and last-entry
                    # which contain message data that can't be JSON serialized
                    if key in ["first-entry", "last-entry"]:
                        # Just extract the message ID (first element)
                        if isinstance(value, list) and len(value) > 0:
                            msg_id = value[0]
                            if isinstance(msg_id, bytes):
                                msg_id = msg_id.decode()
                            value = msg_id
                        else:
                            value = None
                    elif isinstance(value, bytes):
                        try:
                            value = value.decode()
                        except UnicodeDecodeError:
                            # Binary data that can't be decoded, skip it
                            value = "<binary>"

                    info_dict[key] = value

                # Calculate stream age from last entry (for determining if stream is stale)
                stream_age_seconds = 0
                last_entry_id = info_dict.get("last-entry")
                if last_entry_id:
                    try:
                        # Redis Stream IDs format: "milliseconds-sequence"
                        last_timestamp_ms = int(last_entry_id.split("-")[0])
                        last_timestamp_s = last_timestamp_ms / 1000
                        stream_age_seconds = current_time - last_timestamp_s
                    except (ValueError, IndexError, AttributeError):
                        stream_age_seconds = 0

                # Stream suffix is the immutable recording session id.
                session_id = stream_name.removeprefix("audio:stream:")
                session_data = session_by_id.get(session_id, {})
                client_id = session_data.get("client_id", "")

                # Get session age from associated session (more meaningful than stream age)
                session_age_seconds = 0
                session_idle_seconds = 0
                if session_data:
                    session_age_seconds = session_data.get("age_seconds", 0)
                    session_idle_seconds = session_data.get("idle_seconds", 0)

                # Get consumer groups
                groups = await redis_client.execute_command(
                    "XINFO", "GROUPS", stream_name
                )

                stream_data = {
                    "stream_length": info_dict.get("length", 0),
                    "first_entry_id": info_dict.get("first-entry"),
                    "last_entry_id": last_entry_id,
                    "session_age_seconds": session_age_seconds,  # Age since session started
                    "session_idle_seconds": session_idle_seconds,  # Time since last audio chunk
                    "session_id": session_id,
                    "client_id": client_id,  # Include client_id for reference
                    "consumer_groups": [],
                }

                # Track if stream has any active consumers
                has_active_consumer = False
                min_consumer_idle_ms = float("inf")

                # Parse consumer groups
                for group in groups:
                    group_dict = {}
                    for i in range(0, len(group), 2):
                        key = (
                            group[i].decode()
                            if isinstance(group[i], bytes)
                            else str(group[i])
                        )
                        value = group[i + 1]
                        if isinstance(value, bytes):
                            try:
                                value = value.decode()
                            except UnicodeDecodeError:
                                value = "<binary>"
                        group_dict[key] = value

                    group_name = group_dict.get("name", "unknown")
                    if isinstance(group_name, bytes):
                        group_name = group_name.decode()

                    # Get consumers for this group
                    consumers = await redis_client.execute_command(
                        "XINFO", "CONSUMERS", stream_name, group_name
                    )
                    consumer_list = []
                    consumer_pending_total = 0

                    for consumer in consumers:
                        consumer_dict = {}
                        for i in range(0, len(consumer), 2):
                            key = (
                                consumer[i].decode()
                                if isinstance(consumer[i], bytes)
                                else str(consumer[i])
                            )
                            value = consumer[i + 1]
                            if isinstance(value, bytes):
                                try:
                                    value = value.decode()
                                except UnicodeDecodeError:
                                    value = "<binary>"
                            consumer_dict[key] = value

                        consumer_name = consumer_dict.get("name", "unknown")
                        if isinstance(consumer_name, bytes):
                            consumer_name = consumer_name.decode()

                        consumer_pending = int(consumer_dict.get("pending", 0))
                        consumer_idle_ms = int(consumer_dict.get("idle", 0))
                        consumer_pending_total += consumer_pending

                        # Track minimum idle time
                        min_consumer_idle_ms = min(
                            min_consumer_idle_ms, consumer_idle_ms
                        )

                        # Consumer is active if idle < 5 minutes (300000ms)
                        if consumer_idle_ms < 300000:
                            has_active_consumer = True

                        consumer_list.append(
                            {
                                "name": consumer_name,
                                "pending": consumer_pending,
                                "idle_ms": consumer_idle_ms,
                            }
                        )

                    # Get group-level pending count (may be 0 even if consumers have pending)
                    try:
                        pending = await redis_client.xpending(stream_name, group_name)
                        group_pending_count = int(pending[0]) if pending else 0
                    except Exception:
                        group_pending_count = 0

                    # Use the maximum of group-level pending or sum of consumer pending
                    # (Sometimes group pending is 0 but consumers still have pending messages)
                    effective_pending = max(group_pending_count, consumer_pending_total)

                    stream_data["consumer_groups"].append(
                        {
                            "name": str(group_name),
                            "consumers": consumer_list,
                            "pending": int(effective_pending),
                        }
                    )

                # Determine if stream is active or completed
                # Active: has active consumers OR pending messages OR recent activity (< 5 min)
                # Completed: no active consumers and idle > 5 minutes but < 1 hour
                total_pending = sum(
                    group["pending"] for group in stream_data["consumer_groups"]
                )
                is_active = (
                    has_active_consumer
                    or total_pending > 0
                    or stream_age_seconds < 300  # Less than 5 minutes old
                )

                if is_active:
                    active_streams[stream_name] = stream_data
                else:
                    # Mark as completed (will be cleaned up when > 1 hour old)
                    stream_data["idle_seconds"] = stream_age_seconds
                    completed_streams[stream_name] = stream_data

            except Exception as e:
                # Stream doesn't exist or error getting info
                logger.debug(f"Error processing stream {stream_name}: {e}")
                continue

        # Get RQ queue stats - include all registries
        rq_stats = {
            "transcription_queue": {
                "queued": transcription_queue.count,
                "started": len(transcription_queue.started_job_registry),
                "finished": len(transcription_queue.finished_job_registry),
                "failed": len(transcription_queue.failed_job_registry),
                "canceled": len(transcription_queue.canceled_job_registry),
                "deferred": len(transcription_queue.deferred_job_registry),
            },
            "memory_queue": {
                "queued": memory_queue.count,
                "started": len(memory_queue.started_job_registry),
                "finished": len(memory_queue.finished_job_registry),
                "failed": len(memory_queue.failed_job_registry),
                "canceled": len(memory_queue.canceled_job_registry),
                "deferred": len(memory_queue.deferred_job_registry),
            },
            "default_queue": {
                "queued": default_queue.count,
                "started": len(default_queue.started_job_registry),
                "finished": len(default_queue.finished_job_registry),
                "failed": len(default_queue.failed_job_registry),
                "canceled": len(default_queue.canceled_job_registry),
                "deferred": len(default_queue.deferred_job_registry),
            },
        }

        return {
            "active_sessions": active_sessions,
            "completed_sessions": completed_sessions_from_redis,
            "active_streams": active_streams,
            "completed_streams": completed_streams,
            "stream_health": active_streams,  # Backward compatibility - use active_streams
            "rq_queues": rq_stats,
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"Error getting streaming status: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get streaming status: {str(e)}"},
        )


async def cleanup_old_sessions(request, max_age_seconds: int = 3600):
    """Clean terminal metadata only after the raw audio log is durably drained."""
    try:
        redis_client = request.app.state.redis_audio_stream
        if not redis_client:
            return JSONResponse(
                status_code=503,
                content={"error": "Redis client for audio streaming not initialized"},
            )

        store = SessionStore(redis_client)
        views = [view async for view in store.iter_views()]
        by_stream = {view.stream_name: view for view in views if view.stream_name}
        current_time = time.time()
        cleaned_streams = 0
        stream_details = []

        for stream_key in await redis_client.keys("audio:stream:*"):
            stream_name = (
                stream_key.decode() if isinstance(stream_key, bytes) else stream_key
            )
            view = by_stream.get(stream_name)

            # A momentarily-drained group on an ACTIVE or unknown session is not a
            # terminal state; the producer may append again immediately.
            if view is None or view.status not in (
                SessionStatus.FINALIZING,
                SessionStatus.FINISHED,
            ):
                await redis_client.persist(stream_name)
                stream_details.append(
                    {
                        "stream_name": stream_name,
                        "deleted": False,
                        "reason": "session_not_terminal",
                    }
                )
                continue

            await redis_client.persist(stream_name)
            decision = await delete_stream_if_durable(
                redis_client,
                stream_name,
                required_groups={AUDIO_PERSISTENCE_GROUP},
            )
            cleaned_streams += int(decision.safe_to_delete)
            stream_details.append(
                {
                    "stream_name": stream_name,
                    "deleted": decision.safe_to_delete,
                    "reason": decision.reason,
                }
            )

        cleaned_sessions = 0
        session_details = []
        for view in views:
            age_seconds = current_time - view.started_at
            stream_name = view.stream_name
            stream_exists = bool(stream_name and await redis_client.exists(stream_name))
            should_clean = (
                view.status is SessionStatus.FINISHED
                and age_seconds > max_age_seconds
                and not stream_exists
            )
            if not should_clean:
                continue

            await store.delete(view.session_id)
            cleaned_sessions += 1
            session_details.append(
                {
                    "session_id": view.session_id,
                    "age_seconds": age_seconds,
                    "status": view.status.value,
                }
            )

        return {
            "success": True,
            "cleaned_sessions": cleaned_sessions,
            "cleaned_streams": cleaned_streams,
            "cleaned_session_details": session_details,
            "cleaned_stream_details": stream_details,
            "timestamp": time.time(),
        }
    except Exception as error:
        logger.error(f"Error cleaning up old sessions: {error}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to cleanup old sessions: {str(error)}"},
        )
