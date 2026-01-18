"""
Queue Controller - RQ queue configuration, management and monitoring.

This module provides:
- Queue setup and configuration
- Job statistics and monitoring
- Queue health checks
- Beanie initialization for workers
"""

import asyncio
import os
import logging
import uuid
from datetime import datetime
from typing import Dict, Any, Optional

import redis
from rq import Queue, Worker
from rq.job import Job
from rq.registry import ScheduledJobRegistry, DeferredJobRegistry

from advanced_omi_backend.models.job import JobPriority
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.config_loader import get_service_config

logger = logging.getLogger(__name__)

# Redis connection configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_conn = redis.from_url(REDIS_URL)

# Queue name constants
TRANSCRIPTION_QUEUE = "transcription"
MEMORY_QUEUE = "memory"
AUDIO_QUEUE = "audio"
DEFAULT_QUEUE = "default"

# Centralized list of all queue names
QUEUE_NAMES = [DEFAULT_QUEUE, TRANSCRIPTION_QUEUE, MEMORY_QUEUE, AUDIO_QUEUE]

# Job retention configuration
JOB_RESULT_TTL = int(os.getenv("RQ_RESULT_TTL", 86400))  # 24 hour default

# Create queues with custom result TTL
transcription_queue = Queue(TRANSCRIPTION_QUEUE, connection=redis_conn, default_timeout=86400)  # 24 hours for streaming jobs
memory_queue = Queue(MEMORY_QUEUE, connection=redis_conn, default_timeout=300)
audio_queue = Queue(AUDIO_QUEUE, connection=redis_conn, default_timeout=86400)  # 24 hours for all-day sessions
default_queue = Queue(DEFAULT_QUEUE, connection=redis_conn, default_timeout=300)


def get_queue(queue_name: str = DEFAULT_QUEUE) -> Queue:
    """Get an RQ queue by name."""
    queues = {
        TRANSCRIPTION_QUEUE: transcription_queue,
        MEMORY_QUEUE: memory_queue,
        AUDIO_QUEUE: audio_queue,
        DEFAULT_QUEUE: default_queue,
    }
    return queues.get(queue_name, default_queue)


def get_job_stats() -> Dict[str, Any]:
    """Get statistics about jobs in all queues matching frontend expectations."""
    total_jobs = 0
    queued_jobs = 0
    processing_jobs = 0
    completed_jobs = 0
    failed_jobs = 0
    cancelled_jobs = 0
    deferred_jobs = 0  # Jobs waiting for dependencies (depends_on)

    for queue_name in QUEUE_NAMES:
        queue = get_queue(queue_name)

        queued_jobs += len(queue)
        processing_jobs += len(queue.started_job_registry)
        completed_jobs += len(queue.finished_job_registry)
        failed_jobs += len(queue.failed_job_registry)
        cancelled_jobs += len(queue.canceled_job_registry)
        deferred_jobs += len(queue.deferred_job_registry)

    total_jobs = queued_jobs + processing_jobs + completed_jobs + failed_jobs + cancelled_jobs + deferred_jobs

    return {
        "total_jobs": total_jobs,
        "queued_jobs": queued_jobs,
        "processing_jobs": processing_jobs,
        "completed_jobs": completed_jobs,
        "failed_jobs": failed_jobs,
        "cancelled_jobs": cancelled_jobs,
        "deferred_jobs": deferred_jobs,
        "timestamp": datetime.utcnow().isoformat()
    }


def get_jobs(
    limit: int = 20,
    offset: int = 0,
    queue_name: str = None,
    job_type: str = None,
    client_id: str = None
) -> Dict[str, Any]:
    """
    Get jobs from a specific queue or all queues with optional filtering.

    Args:
        limit: Maximum number of jobs to return
        offset: Number of jobs to skip
        queue_name: Specific queue name or None for all queues
        job_type: Filter by job type (matches func_name, e.g., "speech_detection")
        client_id: Filter by client_id in job meta (partial match)

    Returns:
        Dict with jobs list and pagination metadata matching frontend expectations
    """
    logger.info(f"🔍 DEBUG get_jobs: Filtering - queue_name={queue_name}, job_type={job_type}, client_id={client_id}")
    all_jobs = []
    seen_job_ids = set()  # Track which job IDs we've already processed to avoid duplicates

    queues_to_check = [queue_name] if queue_name else QUEUE_NAMES
    logger.info(f"🔍 DEBUG get_jobs: Checking queues: {queues_to_check}")

    for qname in queues_to_check:
        queue = get_queue(qname)

        # Collect jobs from all registries
        registries = [
            (queue.job_ids, "queued"),
            (queue.started_job_registry.get_job_ids(), "processing"),
            (queue.finished_job_registry.get_job_ids(), "completed"),
            (queue.failed_job_registry.get_job_ids(), "failed"),
            (queue.deferred_job_registry.get_job_ids(), "deferred"),  # Jobs waiting for dependencies
        ]

        for job_ids, status in registries:
            for job_id in job_ids:
                # Skip if we've already processed this job_id (prevents duplicates across registries)
                if job_id in seen_job_ids:
                    continue
                seen_job_ids.add(job_id)

                try:
                    job = Job.fetch(job_id, connection=redis_conn)

                    # Extract user_id from kwargs if present
                    user_id = job.kwargs.get("user_id", "") if job.kwargs else ""

                    # Extract just the function name (e.g., "listen_for_speech_job" from "module.listen_for_speech_job")
                    func_name = job.func_name.split('.')[-1] if job.func_name else "unknown"

                    # Debug: Log job details before filtering
                    logger.debug(f"🔍 DEBUG get_jobs: Job {job_id} - func_name={func_name}, full_func_name={job.func_name}, meta_client_id={job.meta.get('client_id', '') if job.meta else ''}, status={status}")

                    # Apply job_type filter
                    if job_type and job_type not in func_name:
                        logger.debug(f"🔍 DEBUG get_jobs: Filtered out {job_id} - job_type '{job_type}' not in func_name '{func_name}'")
                        continue

                    # Apply client_id filter (partial match in meta)
                    if client_id:
                        job_client_id = job.meta.get("client_id", "") if job.meta else ""
                        if client_id not in job_client_id:
                            logger.debug(f"🔍 DEBUG get_jobs: Filtered out {job_id} - client_id '{client_id}' not in job_client_id '{job_client_id}'")
                            continue

                    logger.debug(f"🔍 DEBUG get_jobs: Including job {job_id} in results")

                    all_jobs.append({
                        "job_id": job.id,
                        "job_type": func_name,
                        "user_id": user_id,
                        "status": status,
                        "priority": "normal",  # RQ doesn't track priority in metadata
                        "data": {
                            "description": job.description or "",
                            "queue": qname,
                        },
                        "result": job.result if hasattr(job, 'result') else None,
                        "meta": job.meta if job.meta else {},  # Include job metadata
                        "error_message": str(job.exc_info) if job.exc_info else None,
                        "created_at": job.created_at.isoformat() if job.created_at else None,
                        "started_at": job.started_at.isoformat() if job.started_at else None,
                        "completed_at": job.ended_at.isoformat() if job.ended_at else None,
                        "retry_count": job.retries_left if hasattr(job, 'retries_left') else 0,
                        "max_retries": 3,  # Default max retries
                        "progress_percent": 0,  # RQ doesn't track progress by default
                        "progress_message": "",
                    })
                except Exception as e:
                    logger.error(f"Error fetching job {job_id}: {e}")

    # Sort by created_at (most recent first)
    all_jobs.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    # Paginate
    total_jobs = len(all_jobs)
    paginated_jobs = all_jobs[offset:offset + limit]
    has_more = (offset + limit) < total_jobs

    logger.info(f"🔍 DEBUG get_jobs: Found {total_jobs} matching jobs (returning {len(paginated_jobs)} after pagination)")

    return {
        "jobs": paginated_jobs,
        "pagination": {
            "total": total_jobs,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
        }
    }


def all_jobs_complete_for_client(client_id: str) -> bool:
    """
    Check if all jobs associated with a client are in terminal states.

    Checks jobs with client_id in job.meta.
    Traverses dependency chains to include dependent jobs.

    Args:
        client_id: The client device identifier to check jobs for

    Returns:
        True if all jobs are complete (or no jobs found), False if any job is still processing
    """
    processed_job_ids = set()

    def is_job_complete(job):
        """Recursively check if job and all its dependents are terminal."""
        if job.id in processed_job_ids:
            return True
        processed_job_ids.add(job.id)

        # Check if this job is terminal
        if not (job.is_finished or job.is_failed or job.is_canceled):
            logger.debug(f"Job {job.id} ({job.func_name}) is not terminal")
            return False

        # Check dependent jobs
        for dep_id in (job.dependent_ids or []):
            try:
                dep_job = Job.fetch(dep_id, connection=redis_conn)
                if not is_job_complete(dep_job):
                    return False
            except Exception as e:
                logger.debug(f"Error fetching dependent job {dep_id}: {e}")

        return True

    # Find all jobs for this client
    all_queues = [transcription_queue, memory_queue, audio_queue, default_queue]
    for queue in all_queues:
        registries = [
            queue.job_ids,
            queue.started_job_registry.get_job_ids(),
            queue.finished_job_registry.get_job_ids(),
            queue.failed_job_registry.get_job_ids(),
            queue.canceled_job_registry.get_job_ids(),
            ScheduledJobRegistry(queue=queue).get_job_ids(),
            DeferredJobRegistry(queue=queue).get_job_ids(),
        ]

        for job_ids in registries:
            for job_id in job_ids:
                try:
                    job = Job.fetch(job_id, connection=redis_conn)

                    # Only check jobs with client_id in meta
                    if job.meta and job.meta.get('client_id') == client_id:
                        if not is_job_complete(job):
                            return False
                except Exception as e:
                    logger.debug(f"Error checking job {job_id}: {e}")

    return True


def start_streaming_jobs(
    session_id: str,
    user_id: str,
    client_id: str
) -> Dict[str, str]:
    """
    Enqueue jobs for streaming audio session (initial session setup).

    This starts the parallel job processing for a NEW streaming session:
    1. Speech detection job - monitors transcription results for speech
    2. Audio persistence job - writes audio chunks to WAV file (file rotation per conversation)

    Args:
        session_id: Stream session ID (equals client_id for streaming)
        user_id: User identifier
        client_id: Client identifier

    Returns:
        Dict with job IDs: {'speech_detection': job_id, 'audio_persistence': job_id}

    Note: user_email is fetched from the database when needed.
    """
    from advanced_omi_backend.workers.transcription_jobs import stream_speech_detection_job
    from advanced_omi_backend.workers.audio_jobs import audio_streaming_persistence_job

    # Enqueue speech detection job
    speech_job = transcription_queue.enqueue(
        stream_speech_detection_job,
        session_id,
        user_id,
        client_id,
        job_timeout=86400,  # 24 hours for all-day sessions
        ttl=None,  # No pre-run expiry (job can wait indefinitely in queue)
        result_ttl=JOB_RESULT_TTL,  # Cleanup AFTER completion
        failure_ttl=86400,  # Cleanup failed jobs after 24h
        job_id=f"speech-detect_{session_id[:12]}",
        description=f"Listening for speech...",
        meta={'client_id': client_id, 'session_level': True}
    )
    # Log job enqueue with TTL information for debugging
    actual_ttl = redis_conn.ttl(f"rq:job:{speech_job.id}")
    logger.info(f"📥 RQ: Enqueued speech detection job {speech_job.id}")
    logger.info(
        f"🔍 Job enqueue details: ID={speech_job.id}, "
        f"job_timeout={speech_job.timeout}, result_ttl={speech_job.result_ttl}, "
        f"failure_ttl={speech_job.failure_ttl}, redis_key_ttl={actual_ttl}, "
        f"queue_length={transcription_queue.count}, client_id={client_id}"
    )

    # Store job ID for cleanup (keyed by client_id for easy WebSocket cleanup)
    try:
        redis_conn.set(f"speech_detection_job:{client_id}", speech_job.id, ex=86400)  # 24 hour TTL
        logger.info(f"📌 Stored speech detection job ID for client {client_id}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to store job ID for {client_id}: {e}")

    # Enqueue audio persistence job on dedicated audio queue
    # NOTE: This job handles file rotation for multiple conversations automatically
    # Runs for entire session, not tied to individual conversations
    audio_job = audio_queue.enqueue(
        audio_streaming_persistence_job,
        session_id,
        user_id,
        client_id,
        job_timeout=86400,  # 24 hours for all-day sessions
        ttl=None,  # No pre-run expiry (job can wait indefinitely in queue)
        result_ttl=JOB_RESULT_TTL,  # Cleanup AFTER completion
        failure_ttl=86400,  # Cleanup failed jobs after 24h
        job_id=f"audio-persist_{session_id[:12]}",
        description=f"Audio persistence for session {session_id[:12]}",
        meta={'client_id': client_id, 'session_level': True}  # Mark as session-level job
    )
    # Log job enqueue with TTL information for debugging
    actual_ttl = redis_conn.ttl(f"rq:job:{audio_job.id}")
    logger.info(f"📥 RQ: Enqueued audio persistence job {audio_job.id} on audio queue")
    logger.info(
        f"🔍 Job enqueue details: ID={audio_job.id}, "
        f"job_timeout={audio_job.timeout}, result_ttl={audio_job.result_ttl}, "
        f"failure_ttl={audio_job.failure_ttl}, redis_key_ttl={actual_ttl}, "
        f"queue_length={audio_queue.count}, client_id={client_id}"
    )

    return {
        'speech_detection': speech_job.id,
        'audio_persistence': audio_job.id
    }


def start_post_conversation_jobs(
    conversation_id: str,
    user_id: str,
    transcript_version_id: Optional[str] = None,
    depends_on_job = None,
    client_id: Optional[str] = None
) -> Dict[str, str]:
    """
    Start post-conversation processing jobs after conversation is created.

    This creates the standard processing chain after a conversation is created:
    1. Speaker recognition job - Identifies speakers in audio segments
    2. Memory extraction job - Extracts memories from conversation
    3. Title/summary generation job - Generates title and summary
    4. Event dispatch job - Triggers conversation.complete plugins

    Note: Batch transcription removed - streaming conversations use streaming transcript.
    For file uploads, batch transcription must be enqueued separately before calling this function.

    Args:
        conversation_id: Conversation identifier
        user_id: User identifier
        transcript_version_id: Transcript version ID (auto-generated if None)
        depends_on_job: Optional job dependency for first job (e.g., transcription for file uploads)
        client_id: Client ID for UI tracking

    Returns:
        Dict with job IDs for speaker_recognition, memory, title_summary, event_dispatch
    """
    from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job
    from advanced_omi_backend.workers.memory_jobs import process_memory_job
    from advanced_omi_backend.workers.conversation_jobs import generate_title_summary_job, dispatch_conversation_complete_event_job

    version_id = transcript_version_id or str(uuid.uuid4())

    # Build job metadata (include client_id if provided for UI tracking)
    job_meta = {'conversation_id': conversation_id}
    if client_id:
        job_meta['client_id'] = client_id

    # Check if speaker recognition is enabled
    speaker_config = get_service_config('speaker_recognition')
    speaker_enabled = speaker_config.get('enabled', True)  # Default to True for backward compatibility

    # Step 1: Speaker recognition job (conditional - only if enabled)
    speaker_dependency = depends_on_job  # Start with upstream dependency (transcription if file upload)
    speaker_job = None

    if speaker_enabled:
        speaker_job_id = f"speaker_{conversation_id[:12]}"
        logger.info(f"🔍 DEBUG: Creating speaker job with job_id={speaker_job_id}, conversation_id={conversation_id[:12]}")

        speaker_job = transcription_queue.enqueue(
            recognise_speakers_job,
            conversation_id,
            version_id,
            job_timeout=1200,  # 20 minutes
            result_ttl=JOB_RESULT_TTL,
            depends_on=speaker_dependency,
            job_id=speaker_job_id,
            description=f"Speaker recognition for conversation {conversation_id[:8]}",
            meta=job_meta
        )
        speaker_dependency = speaker_job  # Chain for next jobs
        if depends_on_job:
            logger.info(f"📥 RQ: Enqueued speaker recognition job {speaker_job.id}, meta={speaker_job.meta} (depends on {depends_on_job.id})")
        else:
            logger.info(f"📥 RQ: Enqueued speaker recognition job {speaker_job.id}, meta={speaker_job.meta} (no dependencies, starts immediately)")
    else:
        logger.info(f"⏭️  Speaker recognition disabled, skipping speaker job for conversation {conversation_id[:8]}")

    # Step 2: Memory extraction job
    # Depends on speaker job if it was created, otherwise depends on upstream (transcription or nothing)
    memory_job_id = f"memory_{conversation_id[:12]}"
    logger.info(f"🔍 DEBUG: Creating memory job with job_id={memory_job_id}, conversation_id={conversation_id[:12]}")

    memory_job = memory_queue.enqueue(
        process_memory_job,
        conversation_id,
        job_timeout=900,  # 15 minutes
        result_ttl=JOB_RESULT_TTL,
        depends_on=speaker_dependency,  # Either speaker_job or upstream dependency
        job_id=memory_job_id,
        description=f"Memory extraction for conversation {conversation_id[:8]}",
        meta=job_meta
    )
    if speaker_job:
        logger.info(f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (depends on speaker job {speaker_job.id})")
    elif depends_on_job:
        logger.info(f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (depends on {depends_on_job.id})")
    else:
        logger.info(f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (no dependencies, starts immediately)")

    # Step 3: Title/summary generation job
    # Depends on speaker job if enabled, otherwise on upstream dependency
    title_job_id = f"title_summary_{conversation_id[:12]}"
    logger.info(f"🔍 DEBUG: Creating title/summary job with job_id={title_job_id}, conversation_id={conversation_id[:12]}")

    title_summary_job = default_queue.enqueue(
        generate_title_summary_job,
        conversation_id,
        job_timeout=300,  # 5 minutes
        result_ttl=JOB_RESULT_TTL,
        depends_on=speaker_dependency,  # Depends on speaker job if enabled, NOT memory job
        job_id=title_job_id,
        description=f"Generate title and summary for conversation {conversation_id[:8]}",
        meta=job_meta
    )
    if speaker_job:
        logger.info(f"📥 RQ: Enqueued title/summary job {title_summary_job.id}, meta={title_summary_job.meta} (depends on speaker job {speaker_job.id})")
    elif depends_on_job:
        logger.info(f"📥 RQ: Enqueued title/summary job {title_summary_job.id}, meta={title_summary_job.meta} (depends on {depends_on_job.id})")
    else:
        logger.info(f"📥 RQ: Enqueued title/summary job {title_summary_job.id}, meta={title_summary_job.meta} (no dependencies, starts immediately)")

    # Step 5: Dispatch conversation.complete event (runs after both memory and title/summary complete)
    # This ensures plugins receive the event after all processing is done
    event_job_id = f"event_complete_{conversation_id[:12]}"
    logger.info(f"🔍 DEBUG: Creating conversation complete event job with job_id={event_job_id}, conversation_id={conversation_id[:12]}")

    # Event job depends on both memory and title/summary jobs completing
    # Use RQ's depends_on list to wait for both
    event_dispatch_job = default_queue.enqueue(
        dispatch_conversation_complete_event_job,
        conversation_id,
        client_id or "",
        user_id,
        job_timeout=120,  # 2 minutes
        result_ttl=JOB_RESULT_TTL,
        depends_on=[memory_job, title_summary_job],  # Wait for both parallel jobs
        job_id=event_job_id,
        description=f"Dispatch conversation complete event for {conversation_id[:8]}",
        meta=job_meta
    )
    logger.info(f"📥 RQ: Enqueued conversation complete event job {event_dispatch_job.id}, meta={event_dispatch_job.meta} (depends on {memory_job.id} and {title_summary_job.id})")

    return {
        'speaker_recognition': speaker_job.id,
        'memory': memory_job.id,
        'title_summary': title_summary_job.id,
        'event_dispatch': event_dispatch_job.id
    }




def get_queue_health() -> Dict[str, Any]:
    """Get health status of all queues and workers."""
    health = {
        "queues": {},
        "workers": [],
        "redis_connection": "unknown",
        "total_workers": 0,
        "active_workers": 0,
        "idle_workers": 0,
    }

    # Check Redis connection
    try:
        redis_conn.ping()
        health["redis_connection"] = "healthy"
    except Exception as e:
        health["redis_connection"] = f"unhealthy: {e}"
        return health

    # Check each queue
    for queue_name in QUEUE_NAMES:
        queue = get_queue(queue_name)
        health["queues"][queue_name] = {
            "count": len(queue),
            "failed_count": len(queue.failed_job_registry),
            "finished_count": len(queue.finished_job_registry),
            "started_count": len(queue.started_job_registry),
        }

    # Check workers
    workers = Worker.all(connection=redis_conn)
    health["total_workers"] = len(workers)

    for worker in workers:
        state = worker.get_state()
        current_job = worker.get_current_job_id()

        # Count active vs idle workers
        if current_job or state == "busy":
            health["active_workers"] += 1
        else:
            health["idle_workers"] += 1

        health["workers"].append({
            "name": worker.name,
            "state": state,
            "queues": [q.name for q in worker.queues],
            "current_job": current_job,
        })

    return health

# needs tidying but works for now
async def cleanup_stuck_stream_workers(request):
    """Clean up stuck Redis Stream consumers and pending messages from all active streams."""
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

        cleanup_results = {}
        total_cleaned = 0
        total_deleted_consumers = 0
        total_deleted_streams = 0
        current_time = time.time()

        # Discover all audio streams (per-client streams)
        stream_keys = await redis_client.keys("audio:stream:*")

        for stream_key in stream_keys:
            stream_name = stream_key.decode() if isinstance(stream_key, bytes) else stream_key

            try:
                # First check stream age - delete old streams (>1 hour) immediately
                stream_info = await redis_client.execute_command('XINFO', 'STREAM', stream_name)

                # Parse stream info
                info_dict = {}
                for i in range(0, len(stream_info), 2):
                    key_name = stream_info[i].decode() if isinstance(stream_info[i], bytes) else str(stream_info[i])
                    info_dict[key_name] = stream_info[i+1]

                stream_length = int(info_dict.get("length", 0))
                last_entry = info_dict.get("last-entry")

                # Check if stream is old
                should_delete_stream = False
                stream_age = 0

                if stream_length == 0:
                    should_delete_stream = True
                    stream_age = 0
                elif last_entry and isinstance(last_entry, list) and len(last_entry) > 0:
                    try:
                        last_id = last_entry[0]
                        if isinstance(last_id, bytes):
                            last_id = last_id.decode()
                        last_timestamp_ms = int(last_id.split('-')[0])
                        last_timestamp_s = last_timestamp_ms / 1000
                        stream_age = current_time - last_timestamp_s

                        # Delete streams older than 1 hour (3600 seconds)
                        if stream_age > 3600:
                            should_delete_stream = True
                    except (ValueError, IndexError):
                        pass

                if should_delete_stream:
                    await redis_client.delete(stream_name)
                    total_deleted_streams += 1
                    cleanup_results[stream_name] = {
                        "message": f"Deleted old stream (age: {stream_age:.0f}s, length: {stream_length})",
                        "cleaned": 0,
                        "deleted_consumers": 0,
                        "deleted_stream": True,
                        "stream_age": stream_age
                    }
                    continue

                # Get consumer groups
                groups = await redis_client.execute_command('XINFO', 'GROUPS', stream_name)

                if not groups:
                    cleanup_results[stream_name] = {"message": "No consumer groups found", "cleaned": 0, "deleted_stream": False}
                    continue

                # Parse first group
                group_dict = {}
                group = groups[0]
                for i in range(0, len(group), 2):
                    key = group[i].decode() if isinstance(group[i], bytes) else str(group[i])
                    value = group[i+1]
                    if isinstance(value, bytes):
                        try:
                            value = value.decode()
                        except UnicodeDecodeError:
                            value = str(value)
                    group_dict[key] = value

                group_name = group_dict.get("name", "unknown")
                if isinstance(group_name, bytes):
                    group_name = group_name.decode()

                pending_count = int(group_dict.get("pending", 0))

                # Get consumers for this group to check per-consumer pending
                consumers = await redis_client.execute_command('XINFO', 'CONSUMERS', stream_name, group_name)

                cleaned_count = 0
                total_consumer_pending = 0

                # Clean up pending messages for each consumer AND delete dead consumers
                deleted_consumers = 0
                for consumer in consumers:
                    consumer_dict = {}
                    for i in range(0, len(consumer), 2):
                        key = consumer[i].decode() if isinstance(consumer[i], bytes) else str(consumer[i])
                        value = consumer[i+1]
                        if isinstance(value, bytes):
                            try:
                                value = value.decode()
                            except UnicodeDecodeError:
                                value = str(value)
                        consumer_dict[key] = value

                    consumer_name = consumer_dict.get("name", "unknown")
                    if isinstance(consumer_name, bytes):
                        consumer_name = consumer_name.decode()

                    consumer_pending = int(consumer_dict.get("pending", 0))
                    consumer_idle_ms = int(consumer_dict.get("idle", 0))
                    total_consumer_pending += consumer_pending

                    # Check if consumer is dead (idle > 5 minutes = 300000ms)
                    is_dead = consumer_idle_ms > 300000

                    if consumer_pending > 0:
                        logger.info(f"Found {consumer_pending} pending messages for consumer {consumer_name} (idle: {consumer_idle_ms}ms)")

                        # Get pending messages for this specific consumer
                        try:
                            pending_messages = await redis_client.execute_command(
                                'XPENDING', stream_name, group_name, '-', '+', str(consumer_pending), consumer_name
                            )

                            # XPENDING returns flat list: [msg_id, consumer, idle_ms, delivery_count, msg_id, ...]
                            # Parse in groups of 4
                            for i in range(0, len(pending_messages), 4):
                                if i < len(pending_messages):
                                    msg_id = pending_messages[i]
                                    if isinstance(msg_id, bytes):
                                        msg_id = msg_id.decode()

                                    # Claim the message to a cleanup worker
                                    try:
                                        await redis_client.execute_command(
                                            'XCLAIM', stream_name, group_name, 'cleanup-worker', '0', msg_id
                                        )

                                        # Acknowledge it immediately
                                        await redis_client.xack(stream_name, group_name, msg_id)
                                        cleaned_count += 1
                                    except Exception as claim_error:
                                        logger.warning(f"Failed to claim/ack message {msg_id}: {claim_error}")

                        except Exception as consumer_error:
                            logger.error(f"Error processing consumer {consumer_name}: {consumer_error}")

                    # Delete dead consumers (idle > 5 minutes with no pending messages)
                    if is_dead and consumer_pending == 0:
                        try:
                            await redis_client.execute_command(
                                'XGROUP', 'DELCONSUMER', stream_name, group_name, consumer_name
                            )
                            deleted_consumers += 1
                            logger.info(f"🧹 Deleted dead consumer {consumer_name} (idle: {consumer_idle_ms}ms)")
                        except Exception as delete_error:
                            logger.warning(f"Failed to delete consumer {consumer_name}: {delete_error}")

                if total_consumer_pending == 0 and deleted_consumers == 0:
                    cleanup_results[stream_name] = {"message": "No pending messages or dead consumers", "cleaned": 0, "deleted_consumers": 0, "deleted_stream": False}
                    continue

                total_cleaned += cleaned_count
                total_deleted_consumers += deleted_consumers
                cleanup_results[stream_name] = {
                    "message": f"Cleaned {cleaned_count} pending messages, deleted {deleted_consumers} dead consumers",
                    "cleaned": cleaned_count,
                    "deleted_consumers": deleted_consumers,
                    "deleted_stream": False,
                    "original_pending": pending_count
                }

            except Exception as e:
                cleanup_results[stream_name] = {
                    "error": str(e),
                    "cleaned": 0
                }

        return {
            "success": True,
            "total_cleaned": total_cleaned,
            "total_deleted_consumers": total_deleted_consumers,
            "total_deleted_streams": total_deleted_streams,
            "streams": cleanup_results,  # New key for per-stream results
            "providers": cleanup_results,  # Keep for backward compatibility with frontend
            "timestamp": time.time()
        }

    except Exception as e:
        logger.error(f"Error cleaning up stuck workers: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"error": f"Failed to cleanup stuck workers: {str(e)}"}
        )
