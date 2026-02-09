"""
Session controller for handling audio session-related business logic.

This module manages Redis-based audio streaming sessions, including:
- Session metadata and status
- Conversation counts per session
- Session lifecycle tracking
"""

import logging
import time
from typing import Dict, List, Literal, Optional

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def mark_session_complete(
    redis_client,
    session_id: str,
    reason: Literal[
        "websocket_disconnect",
        "user_stopped",
        "inactivity_timeout",
        "max_duration",
        "all_jobs_complete"
    ],
) -> None:
    """
    Single source of truth for marking sessions as complete.

    This function ensures that both 'status' and 'completion_reason' are ALWAYS
    set together atomically, preventing race conditions where workers check status
    before completion_reason is set.

    Args:
        redis_client: Redis async client
        session_id: Session UUID
        reason: Why the session is completing (enforced by type system)

    Usage:
        # WebSocket disconnect
        await mark_session_complete(redis, session_id, "websocket_disconnect")

        # User manually stopped
        await mark_session_complete(redis, session_id, "user_stopped")

        # Inactivity timeout
        await mark_session_complete(redis, session_id, "inactivity_timeout")

        # Max duration reached
        await mark_session_complete(redis, session_id, "max_duration")

        # All jobs finished
        await mark_session_complete(redis, session_id, "all_jobs_complete")
    """
    session_key = f"audio:session:{session_id}"
    mark_time = time.time()
    await redis_client.hset(session_key, mapping={
        "status": "finished",
        "completed_at": str(mark_time),
        "completion_reason": reason
    })
    logger.info(f"✅ Session {session_id[:12]} marked finished: {reason} [TIME: {mark_time:.3f}]")


async def request_conversation_close(
    redis_client,
    session_id: str,
    reason: str = "user_requested",
) -> bool:
    """
    Request closing the current conversation without killing the session.

    Unlike mark_session_complete() which finalizes the entire session,
    this signals open_conversation_job to close just the current conversation
    and trigger post-processing. The session stays active for new conversations.

    Sets 'conversation_close_requested' field on the session hash.
    The open_conversation_job checks this field every poll iteration.

    Args:
        redis_client: Redis async client
        session_id: Session UUID
        reason: Why the conversation is being closed

    Returns:
        True if the close request was set, False if session not found
    """
    session_key = f"audio:session:{session_id}"
    if not await redis_client.exists(session_key):
        return False
    await redis_client.hset(session_key, "conversation_close_requested", reason)
    logger.info(f"🔒 Conversation close requested for session {session_id[:12]}: {reason}")
    return True


async def get_session_info(redis_client, session_id: str) -> Optional[Dict]:
    """
    Get detailed information about a specific session.

    Args:
        redis_client: Redis async client
        session_id: Session UUID

    Returns:
        Dict with session information or None if not found
    """
    try:
        session_key = f"audio:session:{session_id}"
        session_data = await redis_client.hgetall(session_key)

        if not session_data:
            return None

        # Get conversation count for this session
        conversation_count_key = f"session:conversation_count:{session_id}"
        conversation_count_bytes = await redis_client.get(conversation_count_key)
        conversation_count = int(conversation_count_bytes.decode()) if conversation_count_bytes else 0

        started_at = float(session_data.get(b"started_at", b"0"))
        last_chunk_at = float(session_data.get(b"last_chunk_at", b"0"))

        return {
            "session_id": session_id,
            "user_id": session_data.get(b"user_id", b"").decode(),
            "client_id": session_data.get(b"client_id", b"").decode(),
            "provider": session_data.get(b"provider", b"").decode(),
            "mode": session_data.get(b"mode", b"").decode(),
            "status": session_data.get(b"status", b"").decode(),
            "chunks_published": int(session_data.get(b"chunks_published", b"0")),
            "started_at": started_at,
            "last_chunk_at": last_chunk_at,
            "age_seconds": time.time() - started_at,
            "idle_seconds": time.time() - last_chunk_at,
            "conversation_count": conversation_count,
            # Speech detection events
            "last_event": session_data.get(b"last_event", b"").decode(),
            "speech_detected_at": session_data.get(b"speech_detected_at", b"").decode(),
            "speaker_check_status": session_data.get(b"speaker_check_status", b"").decode(),
            "identified_speakers": session_data.get(b"identified_speakers", b"").decode()
        }

    except Exception as e:
        logger.error(f"Error getting session info for {session_id}: {e}")
        return None


async def get_all_sessions(redis_client, limit: int = 100) -> List[Dict]:
    """
    Get information about all active sessions.

    Args:
        redis_client: Redis async client
        limit: Maximum number of sessions to return

    Returns:
        List of session info dictionaries
    """
    try:
        # Get all session keys
        session_keys = []
        cursor = b"0"
        while cursor and len(session_keys) < limit:
            cursor, keys = await redis_client.scan(
                cursor, match="audio:session:*", count=limit
            )
            session_keys.extend(keys[:limit - len(session_keys)])

        # Get info for each session
        sessions = []
        for key in session_keys:
            session_id = key.decode().replace("audio:session:", "")
            session_info = await get_session_info(redis_client, session_id)
            if session_info:
                sessions.append(session_info)

        return sessions

    except Exception as e:
        logger.error(f"Error getting all sessions: {e}")
        return []


async def get_session_conversation_count(redis_client, session_id: str) -> int:
    """
    Get the conversation count for a specific session.

    Args:
        redis_client: Redis async client
        session_id: Session UUID

    Returns:
        Number of conversations created in this session
    """
    try:
        conversation_count_key = f"session:conversation_count:{session_id}"
        conversation_count_bytes = await redis_client.get(conversation_count_key)
        return int(conversation_count_bytes.decode()) if conversation_count_bytes else 0
    except Exception as e:
        logger.error(f"Error getting conversation count for session {session_id}: {e}")
        return 0


async def increment_session_conversation_count(redis_client, session_id: str) -> int:
    """
    Increment and return the conversation count for a session.

    Args:
        redis_client: Redis async client
        session_id: Session UUID

    Returns:
        New conversation count
    """
    try:
        conversation_count_key = f"session:conversation_count:{session_id}"
        count = await redis_client.incr(conversation_count_key)
        await redis_client.expire(conversation_count_key, 3600)  # 1 hour TTL
        logger.info(f"📊 Conversation count for session {session_id}: {count}")
        return count
    except Exception as e:
        logger.error(f"Error incrementing conversation count for session {session_id}: {e}")
        return 0


async def get_streaming_status(request):
    """Get status of active streaming sessions and Redis Streams health."""
    from advanced_omi_backend.controllers.queue_controller import (
        all_jobs_complete_for_client,
        default_queue,
        memory_queue,
        transcription_queue,
    )

    try:
        # Get Redis client from request.app.state (initialized during startup)
        redis_client = request.app.state.redis_audio_stream

        if not redis_client:
            return JSONResponse(
                status_code=503,
                content={"error": "Redis client for audio streaming not initialized"}
            )

        # Get all sessions (both active and completed)
        session_keys = await redis_client.keys("audio:session:*")
        active_sessions = []
        completed_sessions_from_redis = []

        for key in session_keys:
            session_id = key.decode().split(":")[-1]

            # Use session_controller to get complete session info including conversation_count
            session_obj = await get_session_info(redis_client, session_id)
            if not session_obj:
                continue

            status = session_obj.get("status", "")

            # Separate active and completed sessions
            # Check if all jobs are complete (including failed jobs)
            # Note: session_id == client_id in streaming context, but using client_id explicitly
            all_jobs_done = all_jobs_complete_for_client(session_obj.get("client_id"))

            # Session is finished if:
            # 1. Redis status says finished AND all jobs done, OR
            # 2. All jobs are done (even if status isn't finished yet)
            # This ensures sessions with failed jobs move to finished
            if status == "finished" or all_jobs_done:
                if all_jobs_done:
                    # All jobs finished - this is truly a finished session
                    # Update Redis status if it wasn't already marked finished
                    if status != "finished":
                        await mark_session_complete(redis_client, session_id, "all_jobs_complete")

                    # Get additional session data for completed sessions
                    session_key = f"audio:session:{session_id}"
                    session_data = await redis_client.hgetall(session_key)

                    completed_sessions_from_redis.append({
                        "session_id": session_id,
                        "client_id": session_obj.get("client_id", ""),
                        "conversation_id": session_data.get(b"conversation_id", b"").decode() if session_data and b"conversation_id" in session_data else None,
                        "has_conversation": bool(session_data and session_data.get(b"conversation_id", b"")),
                        "action": session_data.get(b"action", b"finished").decode() if session_data and b"action" in session_data else "finished",
                        "reason": session_data.get(b"reason", b"").decode() if session_data and b"reason" in session_data else "",
                        "completed_at": session_obj.get("last_chunk_at", 0),
                        "audio_file": session_data.get(b"audio_file", b"").decode() if session_data and b"audio_file" in session_data else "",
                        "conversation_count": session_obj.get("conversation_count", 0)
                    })
                else:
                    # Status says complete but jobs still processing - keep in active
                    active_sessions.append(session_obj)
            else:
                # This is an active session
                active_sessions.append(session_obj)

        # Get stream health for all streams (per-client streams)
        # Categorize as active or completed based on consumer activity
        active_streams = {}
        completed_streams = {}

        # Create a map of client_id to session for quick lookup
        client_to_session = {}
        for session in active_sessions + completed_sessions_from_redis:
            client_id = session.get("client_id")
            if client_id:
                client_to_session[client_id] = session

        # Discover all audio streams
        stream_keys = await redis_client.keys("audio:stream:*")
        current_time = time.time()

        for stream_key in stream_keys:
            stream_name = stream_key.decode() if isinstance(stream_key, bytes) else stream_key
            try:
                # Check if stream exists
                stream_info = await redis_client.execute_command('XINFO', 'STREAM', stream_name)

                # Parse stream info (returns flat list of key-value pairs)
                info_dict = {}
                for i in range(0, len(stream_info), 2):
                    key = stream_info[i].decode() if isinstance(stream_info[i], bytes) else str(stream_info[i])
                    value = stream_info[i+1]

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
                        last_timestamp_ms = int(last_entry_id.split('-')[0])
                        last_timestamp_s = last_timestamp_ms / 1000
                        stream_age_seconds = current_time - last_timestamp_s
                    except (ValueError, IndexError, AttributeError):
                        stream_age_seconds = 0

                # Extract client_id from stream name (audio:stream:{client_id})
                client_id = stream_name.split(":")[-1] if ":" in stream_name else ""

                # Get session age from associated session (more meaningful than stream age)
                session_age_seconds = 0
                session_idle_seconds = 0
                if client_id and client_id in client_to_session:
                    session_data = client_to_session[client_id]
                    session_age_seconds = session_data.get("age_seconds", 0)
                    session_idle_seconds = session_data.get("idle_seconds", 0)

                # Get consumer groups
                groups = await redis_client.execute_command('XINFO', 'GROUPS', stream_name)

                stream_data = {
                    "stream_length": info_dict.get("length", 0),
                    "first_entry_id": info_dict.get("first-entry"),
                    "last_entry_id": last_entry_id,
                    "session_age_seconds": session_age_seconds,  # Age since session started
                    "session_idle_seconds": session_idle_seconds,  # Time since last audio chunk
                    "client_id": client_id,  # Include client_id for reference
                    "consumer_groups": []
                }

                # Track if stream has any active consumers
                has_active_consumer = False
                min_consumer_idle_ms = float('inf')

                # Parse consumer groups
                for group in groups:
                    group_dict = {}
                    for i in range(0, len(group), 2):
                        key = group[i].decode() if isinstance(group[i], bytes) else str(group[i])
                        value = group[i+1]
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
                    consumers = await redis_client.execute_command('XINFO', 'CONSUMERS', stream_name, group_name)
                    consumer_list = []
                    consumer_pending_total = 0

                    for consumer in consumers:
                        consumer_dict = {}
                        for i in range(0, len(consumer), 2):
                            key = consumer[i].decode() if isinstance(consumer[i], bytes) else str(consumer[i])
                            value = consumer[i+1]
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
                        min_consumer_idle_ms = min(min_consumer_idle_ms, consumer_idle_ms)

                        # Consumer is active if idle < 5 minutes (300000ms)
                        if consumer_idle_ms < 300000:
                            has_active_consumer = True

                        consumer_list.append({
                            "name": consumer_name,
                            "pending": consumer_pending,
                            "idle_ms": consumer_idle_ms
                        })

                    # Get group-level pending count (may be 0 even if consumers have pending)
                    try:
                        pending = await redis_client.xpending(stream_name, group_name)
                        group_pending_count = int(pending[0]) if pending else 0
                    except Exception:
                        group_pending_count = 0

                    # Use the maximum of group-level pending or sum of consumer pending
                    # (Sometimes group pending is 0 but consumers still have pending messages)
                    effective_pending = max(group_pending_count, consumer_pending_total)

                    stream_data["consumer_groups"].append({
                        "name": str(group_name),
                        "consumers": consumer_list,
                        "pending": int(effective_pending)
                    })

                # Determine if stream is active or completed
                # Active: has active consumers OR pending messages OR recent activity (< 5 min)
                # Completed: no active consumers and idle > 5 minutes but < 1 hour
                total_pending = sum(group["pending"] for group in stream_data["consumer_groups"])
                is_active = (
                    has_active_consumer or
                    total_pending > 0 or
                    stream_age_seconds < 300  # Less than 5 minutes old
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
                "deferred": len(transcription_queue.deferred_job_registry)
            },
            "memory_queue": {
                "queued": memory_queue.count,
                "started": len(memory_queue.started_job_registry),
                "finished": len(memory_queue.finished_job_registry),
                "failed": len(memory_queue.failed_job_registry),
                "canceled": len(memory_queue.canceled_job_registry),
                "deferred": len(memory_queue.deferred_job_registry)
            },
            "default_queue": {
                "queued": default_queue.count,
                "started": len(default_queue.started_job_registry),
                "finished": len(default_queue.finished_job_registry),
                "failed": len(default_queue.failed_job_registry),
                "canceled": len(default_queue.canceled_job_registry),
                "deferred": len(default_queue.deferred_job_registry)
            }
        }

        return {
            "active_sessions": active_sessions,
            "completed_sessions": completed_sessions_from_redis,
            "active_streams": active_streams,
            "completed_streams": completed_streams,
            "stream_health": active_streams,  # Backward compatibility - use active_streams
            "rq_queues": rq_stats,
            "timestamp": time.time()
        }

    except Exception as e:
        logger.error(f"Error getting streaming status: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get streaming status: {str(e)}"}
        )


async def cleanup_old_sessions(request, max_age_seconds: int = 3600):
    """Clean up old session tracking metadata and old audio streams from Redis."""
    import time

    from fastapi.responses import JSONResponse

    try:
        # Get Redis client from request.app.state (initialized during startup)
        redis_client = request.app.state.redis_audio_stream

        if not redis_client:
            return JSONResponse(
                status_code=503,
                content={"error": "Redis client for audio streaming not initialized"}
            )

        # Get all session keys
        session_keys = await redis_client.keys("audio:session:*")
        cleaned_sessions = 0
        old_sessions = []

        current_time = time.time()

        for key in session_keys:
            session_data = await redis_client.hgetall(key)
            if not session_data:
                continue

            session_id = key.decode().split(":")[-1]
            started_at = float(session_data.get(b"started_at", b"0"))
            status = session_data.get(b"status", b"").decode()

            age_seconds = current_time - started_at

            # Clean up sessions older than max_age or stuck in "finalizing"
            should_clean = (
                age_seconds > max_age_seconds or
                (status == "finalizing" and age_seconds > 300)  # Finalizing for more than 5 minutes
            )

            if should_clean:
                old_sessions.append({
                    "session_id": session_id,
                    "age_seconds": age_seconds,
                    "status": status
                })
                await redis_client.delete(key)
                cleaned_sessions += 1

        # Also clean up old audio streams (per-client streams that are inactive)
        stream_keys = await redis_client.keys("audio:stream:*")
        cleaned_streams = 0
        old_streams = []

        for stream_key in stream_keys:
            stream_name = stream_key.decode() if isinstance(stream_key, bytes) else stream_key

            try:
                # Check stream info to get last activity
                stream_info = await redis_client.execute_command('XINFO', 'STREAM', stream_name)

                # Parse stream info
                info_dict = {}
                for i in range(0, len(stream_info), 2):
                    key_name = stream_info[i].decode() if isinstance(stream_info[i], bytes) else str(stream_info[i])
                    info_dict[key_name] = stream_info[i+1]

                stream_length = int(info_dict.get("length", 0))
                last_entry = info_dict.get("last-entry")

                # Check stream age via last entry ID (Redis Stream IDs are timestamps)
                should_delete = False
                age_seconds = 0

                if stream_length == 0:
                    # Empty stream - safe to delete
                    should_delete = True
                    reason = "empty"
                elif last_entry and isinstance(last_entry, list) and len(last_entry) > 0:
                    # Extract timestamp from last entry ID
                    last_id = last_entry[0]
                    if isinstance(last_id, bytes):
                        last_id = last_id.decode()

                    # Redis Stream IDs format: "milliseconds-sequence"
                    try:
                        last_timestamp_ms = int(last_id.split('-')[0])
                        last_timestamp_s = last_timestamp_ms / 1000
                        age_seconds = current_time - last_timestamp_s

                        # Delete streams older than max_age regardless of size
                        if age_seconds > max_age_seconds:
                            should_delete = True
                            reason = "old"
                    except (ValueError, IndexError):
                        # If we can't parse timestamp, check if first entry is old
                        first_entry = info_dict.get("first-entry")
                        if first_entry and isinstance(first_entry, list) and len(first_entry) > 0:
                            try:
                                first_id = first_entry[0]
                                if isinstance(first_id, bytes):
                                    first_id = first_id.decode()
                                first_timestamp_ms = int(first_id.split('-')[0])
                                first_timestamp_s = first_timestamp_ms / 1000
                                age_seconds = current_time - first_timestamp_s

                                if age_seconds > max_age_seconds:
                                    should_delete = True
                                    reason = "old_unparseable"
                            except (ValueError, IndexError):
                                pass

                if should_delete:
                    await redis_client.delete(stream_name)
                    cleaned_streams += 1
                    old_streams.append({
                        "stream_name": stream_name,
                        "reason": reason,
                        "age_seconds": age_seconds,
                        "length": stream_length
                    })

            except Exception as e:
                logger.debug(f"Error checking stream {stream_name}: {e}")
                continue

        return {
            "success": True,
            "cleaned_sessions": cleaned_sessions,
            "cleaned_streams": cleaned_streams,
            "cleaned_session_details": old_sessions,
            "cleaned_stream_details": old_streams,
            "timestamp": time.time()
        }

    except Exception as e:
        logger.error(f"Error cleaning up old sessions: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"error": f"Failed to cleanup old sessions: {str(e)}"}
        )
