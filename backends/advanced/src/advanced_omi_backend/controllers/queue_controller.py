"""
Queue Controller - RQ queue configuration, management and monitoring.

This module provides:
- Queue setup and configuration
- Job statistics and monitoring
- Queue health checks
- Beanie initialization for workers
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse
from rq import Queue, Retry, Worker
from rq.exceptions import NoSuchJobError
from rq.job import Dependency, Job, JobStatus
from rq.registry import DeferredJobRegistry, ScheduledJobRegistry

from advanced_omi_backend.config import get_misc_settings
from advanced_omi_backend.config_loader import get_service_config
from advanced_omi_backend.heartbeat import (
    FLEET_HEALTH_KEY,
    evaluate_fleet_health,
    is_rq_worker_fresh,
)
from advanced_omi_backend.redis_factory import create_sync_redis
from advanced_omi_backend.services.memory.audit import MemoryCause, UpdateStrategy
from advanced_omi_backend.services.sse_publisher import publish_sse_event

logger = logging.getLogger(__name__)

# Shared sync Redis connection for RQ (decode_responses=False, as RQ requires).
redis_conn = create_sync_redis()


def _as_allow_failure_dependency(depends_on):
    """Wrap a job (or list of jobs) in a ``Dependency`` with ``allow_failure=True``.

    A plain ``depends_on`` leaves a dependent ``deferred`` forever when an upstream
    job ends up FAILED (e.g. abandoned + retries exhausted). ``allow_failure=True``
    makes RQ *promote* the dependent on upstream failure instead — so the chain
    always drains to the finalizer, which reconciles the real status. Returns
    ``None`` for an empty/all-None input (enqueue with no dependency).
    """
    if depends_on is None:
        return None
    if isinstance(depends_on, Dependency):
        return depends_on
    jobs = list(depends_on) if isinstance(depends_on, (list, tuple)) else [depends_on]
    jobs = [j for j in jobs if j is not None]
    if not jobs:
        return None
    return Dependency(jobs=jobs, allow_failure=True)


def post_conv_enqueue_kwargs(stage: str, meta: dict, depends_on=None) -> dict:
    """Shared enqueue kwargs for every post-conversation chain job.

    Centralises the three things each chain job needs so the three enqueue sites
    (this module, conversation_controller reprocess, enqueue_memory_processing)
    can't drift:

    - ``retry``: bounded immediate re-enqueue, so a transient crash / worker death
      doesn't permanently strand the job (``interval=0`` needs no rq-scheduler).
    - ``on_failure``: emits a visible ``system_event`` + diagnostic breadcrumb when
      the job fails or is abandoned (instead of failing silently).
    - ``meta.failure_stage``: tells that callback which stage this job is.
    - ``depends_on`` wrapped with ``allow_failure=True`` (see helper above).
    """
    # Lazy import: job_callbacks lives in the `workers` package, whose __init__
    # imports back from this module — importing it at module top would create a
    # circular import. By call time (enqueue) all modules are fully loaded.
    from advanced_omi_backend.workers.job_callbacks import on_chain_job_failure

    kwargs: dict = {
        "retry": Retry(max=2, interval=0),
        "on_failure": on_chain_job_failure,
        "meta": {**meta, "failure_stage": stage},
    }
    dep = _as_allow_failure_dependency(depends_on)
    if dep is not None:
        kwargs["depends_on"] = dep
    return kwargs


def get_job_status_from_rq(job: Job) -> str:
    """
    Get job status using RQ's native method.

    Uses job.get_status() which is the Redis Queue standard approach.
    Returns RQ's standard status names.

    Returns one of: queued, started, finished, failed, deferred, scheduled, canceled, stopped

    Raises:
        RuntimeError: If job status is unexpected (should never happen with RQ's method)
    """
    rq_status = job.get_status()

    # RQ returns status as JobStatus enum or string
    # Convert to string if it's an enum
    if isinstance(rq_status, JobStatus):
        status_str = rq_status.value
    else:
        status_str = str(rq_status)

    # Validate it's a known RQ status
    valid_statuses = {
        JobStatus.QUEUED.value,
        JobStatus.STARTED.value,
        JobStatus.FINISHED.value,
        JobStatus.FAILED.value,
        JobStatus.DEFERRED.value,
        JobStatus.SCHEDULED.value,
        JobStatus.CANCELED.value,
        JobStatus.STOPPED.value,
    }

    if status_str not in valid_statuses:
        logger.error(
            f"Job {job.id} has unexpected RQ status: {status_str}. "
            f"This indicates RQ library added a new status we don't know about."
        )
        raise RuntimeError(
            f"Job {job.id} has unknown RQ status: {status_str}. "
            f"Please update get_job_status_from_rq() to handle this new status."
        )

    return status_str


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
transcription_queue = Queue(
    TRANSCRIPTION_QUEUE, connection=redis_conn, default_timeout=86400
)  # 24 hours for streaming jobs
memory_queue = Queue(MEMORY_QUEUE, connection=redis_conn, default_timeout=300)
audio_queue = Queue(
    AUDIO_QUEUE, connection=redis_conn, default_timeout=86400
)  # 24 hours for all-day sessions
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
    """Get statistics about jobs in all queues using RQ standard status names."""
    total_jobs = 0
    queued_jobs = 0
    started_jobs = 0  # RQ standard: "started" not "processing"
    finished_jobs = 0  # RQ standard: "finished" not "completed"
    failed_jobs = 0
    canceled_jobs = 0  # RQ standard: "canceled" not "cancelled"
    deferred_jobs = 0  # Jobs waiting for dependencies (depends_on)

    for queue_name in QUEUE_NAMES:
        queue = get_queue(queue_name)

        queued_jobs += len(queue)
        started_jobs += len(queue.started_job_registry)
        finished_jobs += len(queue.finished_job_registry)
        failed_jobs += len(queue.failed_job_registry)
        canceled_jobs += len(queue.canceled_job_registry)
        deferred_jobs += len(queue.deferred_job_registry)

    total_jobs = (
        queued_jobs
        + started_jobs
        + finished_jobs
        + failed_jobs
        + canceled_jobs
        + deferred_jobs
    )

    return {
        "total_jobs": total_jobs,
        "queued_jobs": queued_jobs,
        "started_jobs": started_jobs,
        "finished_jobs": finished_jobs,
        "failed_jobs": failed_jobs,
        "canceled_jobs": canceled_jobs,
        "deferred_jobs": deferred_jobs,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def get_jobs(
    limit: int = 20,
    offset: int = 0,
    queue_name: Optional[str] = None,
    job_type: Optional[str] = None,
    client_id: Optional[str] = None,
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
    logger.info(
        f"🔍 DEBUG get_jobs: Filtering - queue_name={queue_name}, job_type={job_type}, client_id={client_id}"
    )
    all_jobs = []
    seen_job_ids = (
        set()
    )  # Track which job IDs we've already processed to avoid duplicates

    queues_to_check = [queue_name] if queue_name else QUEUE_NAMES
    logger.info(f"🔍 DEBUG get_jobs: Checking queues: {queues_to_check}")

    for qname in queues_to_check:
        queue = get_queue(qname)

        # Collect jobs from all registries (using RQ standard status names)
        registries = [
            (queue.job_ids, "queued"),
            (
                queue.started_job_registry.get_job_ids(),
                "started",
            ),  # RQ standard, not "processing"
            (
                queue.finished_job_registry.get_job_ids(),
                "finished",
            ),  # RQ standard, not "completed"
            (queue.failed_job_registry.get_job_ids(), "failed"),
            (
                queue.deferred_job_registry.get_job_ids(),
                "deferred",
            ),  # Jobs waiting for dependencies
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
                    func_name = (
                        job.func_name.split(".")[-1] if job.func_name else "unknown"
                    )

                    # Debug: Log job details before filtering
                    logger.debug(
                        f"🔍 DEBUG get_jobs: Job {job_id} - func_name={func_name}, full_func_name={job.func_name}, meta_client_id={job.meta.get('client_id', '') if job.meta else ''}, status={status}"
                    )

                    # Apply job_type filter
                    if job_type and job_type not in func_name:
                        logger.debug(
                            f"🔍 DEBUG get_jobs: Filtered out {job_id} - job_type '{job_type}' not in func_name '{func_name}'"
                        )
                        continue

                    # Apply client_id filter (partial match in meta)
                    if client_id:
                        job_client_id = (
                            job.meta.get("client_id", "") if job.meta else ""
                        )
                        if client_id not in job_client_id:
                            logger.debug(
                                f"🔍 DEBUG get_jobs: Filtered out {job_id} - client_id '{client_id}' not in job_client_id '{job_client_id}'"
                            )
                            continue

                    logger.debug(
                        f"🔍 DEBUG get_jobs: Including job {job_id} in results"
                    )

                    all_jobs.append(
                        {
                            "job_id": job.id,
                            "job_type": func_name,
                            "user_id": user_id,
                            "status": status,
                            "priority": "normal",  # RQ doesn't track priority in metadata
                            "data": {
                                "description": job.description or "",
                                "queue": qname,
                            },
                            "result": job.result if hasattr(job, "result") else None,
                            "meta": (
                                job.meta if job.meta else {}
                            ),  # Include job metadata
                            "error_message": (
                                str(job.exc_info) if job.exc_info else None
                            ),
                            "created_at": (
                                job.created_at.isoformat() if job.created_at else None
                            ),
                            "started_at": (
                                job.started_at.isoformat() if job.started_at else None
                            ),
                            "completed_at": (
                                job.ended_at.isoformat() if job.ended_at else None
                            ),
                            "retry_count": (
                                job.retries_left if hasattr(job, "retries_left") else 0
                            ),
                            "max_retries": 3,  # Default max retries
                            "progress_percent": (job.meta or {})
                            .get("batch_progress", {})
                            .get("percent", 0),
                            "progress_message": (job.meta or {})
                            .get("batch_progress", {})
                            .get("message", ""),
                        }
                    )
                except Exception as e:
                    logger.error(f"Error fetching job {job_id}: {e}")

    # Sort by created_at (most recent first)
    all_jobs.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    # Paginate
    total_jobs = len(all_jobs)
    paginated_jobs = all_jobs[offset : offset + limit]
    has_more = (offset + limit) < total_jobs

    logger.info(
        f"🔍 DEBUG get_jobs: Found {total_jobs} matching jobs (returning {len(paginated_jobs)} after pagination)"
    )

    return {
        "jobs": paginated_jobs,
        "pagination": {
            "total": total_jobs,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
        },
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
        for dep_id in job.dependent_ids or []:
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
                    if job.meta and job.meta.get("client_id") == client_id:
                        if not is_job_complete(job):
                            return False
                except Exception as e:
                    logger.debug(f"Error checking job {job_id}: {e}")

    return True


# Job statuses that mean a speech-detection job is still live, so re-enqueuing
# would create a duplicate detector for the same session.
_LIVE_JOB_STATUSES = {"queued", "started", "deferred", "scheduled"}


def _job_is_live(job_id: str) -> bool:
    """True if the given job exists in Redis and hasn't terminated."""
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        return job.get_status(refresh=True) in _LIVE_JOB_STATUSES
    except NoSuchJobError:
        return False
    except Exception:
        return False


def _speech_detection_job_is_live(job_id: str) -> bool:
    """True if the given speech-detection job exists and hasn't terminated."""
    return _job_is_live(job_id)


def enqueue_audio_persistence(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    always_persist: bool,
) -> str:
    """Single-flight enqueue of the per-session audio-persistence job.

    The persistence job is SESSION-scoped — one consumer for the whole
    ``audio:stream:{client_id}``, surviving WebSocket reconnects. A reconnect
    re-runs ``start_streaming_jobs``; if a persistence job is already live we must
    NOT enqueue a second one. Two persistence jobs share the same Redis consumer
    name (``persistence-{session_id[:8]}``), so each new stream message is
    delivered to only one of them — the audio gets split between the jobs, and the
    speech-detected conversations created after the reconnect find no chunks under
    their id and get deleted as ``audio_chunks_not_ready`` while their transcripts
    are stranded on a different conversation.

    The job id is deterministic per session, so liveness is checked by fetching it
    directly; a short Redis mutex collapses a simultaneous reconnect burst into one
    winner. Returns the live or newly-enqueued job id.
    """
    # Lazy import: circular dependency with the `workers` package (its __init__
    # imports back from this module).
    from advanced_omi_backend.workers.audio_jobs import audio_streaming_persistence_job

    job_id = f"audio-persist_{session_id}"
    lock_key = f"audio_persistence_enqueue_lock:{session_id}"

    if _job_is_live(job_id):
        logger.info(
            f"⏭️ Audio persistence already live for session {session_id[:12]} "
            f"({job_id}) — skipping duplicate enqueue (reconnect)"
        )
        return job_id

    if not redis_conn.set(lock_key, "1", nx=True, ex=15):
        logger.info(
            f"⏭️ Concurrent audio-persistence enqueue in progress for session "
            f"{session_id[:12]} — skipping"
        )
        return job_id
    try:
        # Re-check liveness under the mutex (another caller may have just enqueued).
        if _job_is_live(job_id):
            return job_id

        audio_job = audio_queue.enqueue(
            audio_streaming_persistence_job,
            session_id,
            user_id,
            client_id,
            always_persist,
            job_timeout=86400,  # 24 hours for all-day sessions
            ttl=None,  # No pre-run expiry (job can wait indefinitely in queue)
            result_ttl=JOB_RESULT_TTL,  # Cleanup AFTER completion
            failure_ttl=86400,  # Cleanup failed jobs after 24h
            job_id=job_id,
            description=f"Audio persistence for session {session_id}",
            meta={"client_id": client_id, "session_level": True},
        )
        logger.info(
            f"📥 RQ: Enqueued audio persistence job {audio_job.id} on audio queue "
            f"(session {session_id[:12]})"
        )
        return audio_job.id
    finally:
        redis_conn.delete(lock_key)


def enqueue_speech_detection(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    reason: str = "restart",
    replaces_current: bool = False,
) -> Optional[str]:
    """Single-flight enqueue of the per-session speech-detection job.

    At most ONE speech-detection job may be live per session. Re-enqueuing while
    one is already listening (a WebSocket reconnect, or several conversation-end
    handlers firing at once) previously spawned a swarm of duplicate detectors
    that raced to mark the actively-recording placeholder conversation as
    ``transcription_failed``. This guards every restart path against that.

    The current live job (if any) is tracked in ``speech_detection_job:{session_id}``;
    a short Redis mutex collapses a simultaneous burst of callers into one winner.

    Args:
        replaces_current: set by the off-mode rotation path, where the caller IS
            the current tracked job and is deliberately handing off to a successor
            (so the liveness check would otherwise see itself and skip).

    Returns the live or newly-enqueued job id, or None if it could not enqueue.
    """
    # Lazy import: circular dependency with the `workers` package (its __init__
    # imports back from this module).
    from advanced_omi_backend.workers.transcription_jobs import (
        stream_speech_detection_job,
    )

    job_key = f"speech_detection_job:{session_id}"
    lock_key = f"speech_detection_enqueue_lock:{session_id}"

    def _tracked_id() -> Optional[str]:
        val = redis_conn.get(job_key)
        if isinstance(val, bytes):
            return val.decode()
        if isinstance(val, str):
            return val
        return None

    if not replaces_current:
        existing_id = _tracked_id()
        if existing_id and _speech_detection_job_is_live(existing_id):
            logger.info(
                f"⏭️ Speech detection already live for session {session_id[:12]} "
                f"({existing_id}) — skipping duplicate enqueue (reason={reason})"
            )
            return existing_id

    # Collapse a concurrent-enqueue burst (several end handlers firing together)
    # into a single winner. The loser skips; the winner records the new job in
    # job_key so any later caller sees it live and also skips.
    if not redis_conn.set(lock_key, "1", nx=True, ex=15):
        logger.info(
            f"⏭️ Concurrent speech-detection enqueue in progress for session "
            f"{session_id[:12]} — skipping (reason={reason})"
        )
        return _tracked_id()
    try:
        if not replaces_current:
            # Re-check liveness under the mutex (another caller may have just enqueued).
            existing_id = _tracked_id()
            if existing_id and _speech_detection_job_is_live(existing_id):
                logger.info(
                    f"⏭️ Speech detection became live for session {session_id[:12]} "
                    f"while acquiring lock — skipping (reason={reason})"
                )
                return existing_id

        speech_job = transcription_queue.enqueue(
            stream_speech_detection_job,
            session_id,
            user_id,
            client_id,
            job_timeout=86400,  # 24 hours for all-day sessions
            ttl=None,  # No pre-run expiry (job can wait indefinitely in queue)
            result_ttl=JOB_RESULT_TTL,  # Cleanup AFTER completion
            failure_ttl=86400,  # Cleanup failed jobs after 24h
            job_id=f"speech-detect_{session_id}_{uuid.uuid4().hex[:8]}",
            description="Listening for speech...",
            meta={"client_id": client_id, "session_level": True},
        )
        # Track the live job for both single-flight and WebSocket cleanup.
        redis_conn.set(job_key, speech_job.id, ex=86400)
        logger.info(
            f"📥 RQ: Enqueued speech detection job {speech_job.id} for session "
            f"{session_id[:12]} (reason={reason})"
        )
        return speech_job.id
    finally:
        redis_conn.delete(lock_key)


def start_streaming_jobs(
    session_id: str, user_id: str, client_id: str
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

    Note:
        - user_email is fetched from the database when needed.
        - always_persist setting is read from global config at enqueue time and passed to worker.
    """
    # Lazy import: circular dependency with the `workers` package (its __init__
    # imports back from this module).
    from advanced_omi_backend.workers.audio_jobs import audio_streaming_persistence_job

    # Read always_persist from global config NOW (backend process has fresh config)
    misc_settings = get_misc_settings()
    always_persist = misc_settings.get("always_persist_enabled", False)

    # In "off" live-segmentation mode there is no live transcript to gate on, so batch
    # transcription is the ONLY path to a transcript — and batch reads audio from
    # MongoDB. always_persist=False would mean nothing is persisted (no speech signal
    # to trigger it), so the audio would only ever exist in the soon-trimmed Redis
    # stream and never get transcribed. Force persistence so off mode always has audio
    # for batch; no-speech sessions are still hidden via post-batch soft-delete.
    live_segmentation = misc_settings.get("live_segmentation", "streaming_stt")
    if live_segmentation == "off" and not always_persist:
        logger.info(
            "🔒 live_segmentation=off → forcing always_persist=True so batch "
            "transcription has persisted audio to read"
        )
        always_persist = True

    # Enqueue speech detection job (single-flight: skips if one is already live
    # for this session, e.g. on a WebSocket reconnect mid-session).
    speech_job_id = (
        enqueue_speech_detection(session_id, user_id, client_id, reason="session_start")
        or ""
    )

    # Enqueue audio persistence job on dedicated audio queue (single-flight: a
    # reconnect mid-session reuses the live job instead of starting a second
    # consumer that would split the audio stream). This job handles file rotation
    # for multiple conversations and runs for the entire session.
    audio_job_id = enqueue_audio_persistence(
        session_id, user_id, client_id, always_persist=always_persist
    )

    # Notify frontend that streaming jobs are queued
    publish_sse_event(
        user_id,
        "jobs.queued",
        {
            "client_id": client_id,
            "session_id": session_id,
            "jobs": ["speech_detection", "audio_persistence"],
        },
    )

    return {"speech_detection": speech_job_id, "audio_persistence": audio_job_id}


def _clear_post_conversation_chain(conversation_id: str) -> list:
    """Delete any existing post-conversation chain jobs for a conversation.

    The post-conversation jobs (speaker → memory → title/summary → event) use
    deterministic job_ids keyed on the conversation. When the chain is
    re-triggered (e.g. a transcript reprocess) while a previous chain is still
    ``deferred``, re-enqueuing the same job_id with a *new* ``depends_on`` makes
    RQ **accumulate** dependencies on the existing deferred job rather than
    replacing it. If one upstream dependency then finishes and is evicted from
    Redis before the others resolve, RQ never promotes the dependents and the
    whole chain stays ``deferred`` forever (orphaned).

    Deleting the stale chain first guarantees each re-enqueue starts fresh with
    a single, correct dependency. Jobs that are currently ``started`` are left
    alone so we never yank work out from under a running worker.

    Returns the list of job_ids that were actually deleted (for logging).
    """
    suffix = conversation_id[:12]
    job_ids = [
        f"speaker_{suffix}",
        f"memory_{suffix}",
        f"title_summary_{suffix}",
        f"event_complete_{suffix}",
    ]
    cleared = []
    for job_id in job_ids:
        try:
            job = Job.fetch(job_id, connection=redis_conn)
        except NoSuchJobError:
            continue
        if get_job_status_from_rq(job) == JobStatus.STARTED.value:
            logger.warning(
                f"⚠️  Not clearing post-conversation job {job_id} for "
                f"{conversation_id[:8]}: currently running"
            )
            continue
        try:
            job.delete(remove_from_queue=True)
            cleared.append(job_id)
        except Exception as e:
            logger.error(f"Failed to delete stale chain job {job_id}: {e}")
    if cleared:
        logger.info(
            f"🧹 Cleared {len(cleared)} stale post-conversation job(s) for "
            f"{conversation_id[:8]}: {cleared}"
        )
    return cleared


# Statuses that mean a job is still occupying the chain (not yet terminal).
_IN_FLIGHT_JOB_STATUSES = frozenset(
    {
        JobStatus.QUEUED.value,
        JobStatus.STARTED.value,
        JobStatus.DEFERRED.value,
        JobStatus.SCHEDULED.value,
    }
)


def conversation_edit_chain_in_flight(conversation_id: str) -> Optional[str]:
    """Return an in-flight edit-chain job_id for this conversation, else None.

    Several endpoints edit a conversation by creating a new transcript version and
    enqueuing follow-up work under deterministic job_ids keyed on the conversation:

    - ``reprocess_speakers`` → reprocess_speaker → memory → title_summary
    - annotation apply (``/diarization/{id}/apply``, ``/{id}/apply``) → memory

    Firing any of these again while a previous one is still running spawns overlapping
    work that races on the conversation's full-document ``save()`` — a stale writer can
    clobber a newer version's segments/metadata (e.g. lost speaker labels, an orphaned
    stale ``active`` version). Callers use this as a single-flight guard: if an edit
    chain is already live, don't create a new transcript version or enqueue more work.
    """
    suffix = conversation_id[:12]
    job_ids = [
        f"reprocess_speaker_{suffix}",
        f"memory_{suffix}",
        f"title_summary_{suffix}",
    ]
    for job_id in job_ids:
        try:
            job = Job.fetch(job_id, connection=redis_conn)
        except NoSuchJobError:
            continue
        if get_job_status_from_rq(job) in _IN_FLIGHT_JOB_STATUSES:
            return job_id
    return None


def start_post_conversation_jobs(
    conversation_id: str,
    user_id: str,
    transcript_version_id: Optional[str] = None,
    depends_on_job=None,
    client_id: Optional[str] = None,
    end_reason: str = "file_upload",
    skip_speaker_recognition: bool = False,
    skip_memory_extraction: bool = False,
    memory_cause: MemoryCause = MemoryCause.AUTO_EXTRACTION,
    memory_strategy: UpdateStrategy = UpdateStrategy.FULL,
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
        end_reason: Reason conversation ended (e.g., 'file_upload', 'websocket_disconnect', 'user_stopped')
        skip_speaker_recognition: Skip the speaker step even when enabled — used
            by split/merge, whose transcripts already carry speaker labels
        skip_memory_extraction: Skip memory extraction even when globally enabled —
            used for annotation/training datasets that should not enter user memory

    Returns:
        Dict with job IDs for speaker_recognition, memory, title_summary, event_dispatch
    """
    # Lazy import: circular dependency with the `workers` package (its __init__
    # imports back from this module).
    from advanced_omi_backend.workers.conversation_jobs import (
        dispatch_conversation_complete_event_job,
        generate_title_summary_job,
    )
    from advanced_omi_backend.workers.memory_jobs import process_memory_job
    from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job

    # Re-triggering the chain (e.g. transcript reprocess) must not stack new
    # dependencies onto a previously-deferred chain — that orphans it forever.
    # Clear any stale chain jobs first so each enqueue below starts fresh.
    _clear_post_conversation_chain(conversation_id)

    version_id = transcript_version_id or str(uuid.uuid4())

    # Build job metadata (include client_id if provided for UI tracking)
    job_meta = {"conversation_id": conversation_id}
    if client_id:
        job_meta["client_id"] = client_id

    # Check if speaker recognition is enabled
    speaker_config = get_service_config("speaker_recognition")
    speaker_enabled = speaker_config.get(
        "enabled", True
    )  # Default to True for backward compatibility

    # Step 1: Speaker recognition job (conditional - only if enabled)
    speaker_dependency = (
        depends_on_job  # Start with upstream dependency (transcription if file upload)
    )
    speaker_job = None

    if speaker_enabled and skip_speaker_recognition:
        logger.info(
            f"⏭️  Speaker recognition skipped by caller for conversation {conversation_id[:8]}"
        )
        speaker_enabled = False

    if speaker_enabled:
        speaker_job_id = f"speaker_{conversation_id[:12]}"
        logger.info(
            f"🔍 DEBUG: Creating speaker job with job_id={speaker_job_id}, conversation_id={conversation_id[:12]}"
        )

        speaker_job = transcription_queue.enqueue(
            recognise_speakers_job,
            conversation_id,
            version_id,
            job_timeout=1200,  # 20 minutes
            result_ttl=JOB_RESULT_TTL,
            job_id=speaker_job_id,
            description=f"Speaker recognition for conversation {conversation_id[:8]}",
            **post_conv_enqueue_kwargs(
                "speaker", job_meta, depends_on=speaker_dependency
            ),
        )
        speaker_dependency = speaker_job  # Chain for next jobs
        if depends_on_job:
            logger.info(
                f"📥 RQ: Enqueued speaker recognition job {speaker_job.id}, meta={speaker_job.meta} (depends on {depends_on_job.id})"
            )
        else:
            logger.info(
                f"📥 RQ: Enqueued speaker recognition job {speaker_job.id}, meta={speaker_job.meta} (no dependencies, starts immediately)"
            )
    else:
        logger.info(
            f"⏭️  Speaker recognition disabled, skipping speaker job for conversation {conversation_id[:8]}"
        )

    # Step 2: Memory extraction job (conditional - only if enabled)
    # Check if memory extraction is enabled
    memory_config = get_service_config("memory.extraction")
    memory_enabled = memory_config.get(
        "enabled", True
    )  # Default to True for backward compatibility
    if memory_enabled and skip_memory_extraction:
        logger.info(
            f"⏭️  Memory extraction skipped by caller for conversation {conversation_id[:8]}"
        )
        memory_enabled = False

    memory_job = None
    if memory_enabled:
        # Depends on speaker job if it was created, otherwise depends on upstream (transcription or nothing)
        memory_job_id = f"memory_{conversation_id[:12]}"
        logger.info(
            f"🔍 DEBUG: Creating memory job with job_id={memory_job_id}, conversation_id={conversation_id[:12]}"
        )

        # Memory job carries provenance (cause/strategy) on top of the shared meta.
        memory_meta = {
            **job_meta,
            "cause": memory_cause.value,
            "strategy": memory_strategy.value,
        }
        memory_job = memory_queue.enqueue(
            process_memory_job,
            conversation_id,
            job_timeout=900,  # 15 minutes
            result_ttl=JOB_RESULT_TTL,
            job_id=memory_job_id,
            description=f"Memory extraction for conversation {conversation_id[:8]}",
            # Either speaker_job or upstream dependency
            **post_conv_enqueue_kwargs(
                "memory", memory_meta, depends_on=speaker_dependency
            ),
        )
        if speaker_job:
            logger.info(
                f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (depends on speaker job {speaker_job.id})"
            )
        elif depends_on_job:
            logger.info(
                f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (depends on {depends_on_job.id})"
            )
        else:
            logger.info(
                f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (no dependencies, starts immediately)"
            )
    else:
        logger.info(
            f"⏭️  Memory extraction disabled, skipping memory job for conversation {conversation_id[:8]}"
        )

    # Step 3: Title/summary generation job
    # Depends on memory job to avoid race condition (both jobs save the conversation document)
    # and to ensure fresh memories are available for context-enriched summaries
    title_dependency = memory_job if memory_job else speaker_dependency
    title_job_id = f"title_summary_{conversation_id[:12]}"
    logger.info(
        f"🔍 DEBUG: Creating title/summary job with job_id={title_job_id}, conversation_id={conversation_id[:12]}"
    )

    title_summary_job = default_queue.enqueue(
        generate_title_summary_job,
        conversation_id,
        job_timeout=300,  # 5 minutes
        result_ttl=JOB_RESULT_TTL,
        job_id=title_job_id,
        description=f"Generate title and summary for conversation {conversation_id[:8]}",
        **post_conv_enqueue_kwargs(
            "title_summary", job_meta, depends_on=title_dependency
        ),
    )
    if memory_job:
        logger.info(
            f"📥 RQ: Enqueued title/summary job {title_summary_job.id}, meta={title_summary_job.meta} (depends on memory job {memory_job.id})"
        )
    elif speaker_job:
        logger.info(
            f"📥 RQ: Enqueued title/summary job {title_summary_job.id}, meta={title_summary_job.meta} (depends on speaker job {speaker_job.id})"
        )
    elif depends_on_job:
        logger.info(
            f"📥 RQ: Enqueued title/summary job {title_summary_job.id}, meta={title_summary_job.meta} (depends on {depends_on_job.id})"
        )
    else:
        logger.info(
            f"📥 RQ: Enqueued title/summary job {title_summary_job.id}, meta={title_summary_job.meta} (no dependencies, starts immediately)"
        )

    # Step 5: Dispatch conversation.complete event (runs after both memory and title/summary complete)
    # This ensures plugins receive the event after all processing is done
    event_job_id = f"event_complete_{conversation_id[:12]}"
    logger.info(
        f"🔍 DEBUG: Creating conversation complete event job with job_id={event_job_id}, conversation_id={conversation_id[:12]}"
    )

    # Event job depends on memory and title/summary jobs that were actually enqueued
    # Build dependency list excluding None values
    event_dependencies = []
    if memory_job:
        event_dependencies.append(memory_job)
    if title_summary_job:
        event_dependencies.append(title_summary_job)

    # Enqueue event dispatch job (may have no dependencies if all jobs were skipped)
    event_dispatch_job = default_queue.enqueue(
        dispatch_conversation_complete_event_job,
        conversation_id,
        client_id or "",
        user_id,
        end_reason,  # Use the end_reason parameter (defaults to 'file_upload' for backward compatibility)
        job_timeout=120,  # 2 minutes
        result_ttl=JOB_RESULT_TTL,
        job_id=event_job_id,
        description=f"Dispatch conversation complete event ({end_reason}) for {conversation_id[:8]}",
        # Wait for whichever upstream jobs were enqueued; allow_failure so this
        # finalizer still runs (and reconciles status) even if one failed.
        **post_conv_enqueue_kwargs(
            "event_complete", job_meta, depends_on=event_dependencies or None
        ),
    )

    # Log event dispatch dependencies
    if event_dependencies:
        dep_ids = [job.id for job in event_dependencies]
        logger.info(
            f"📥 RQ: Enqueued conversation complete event job {event_dispatch_job.id}, meta={event_dispatch_job.meta} (depends on {', '.join(dep_ids)})"
        )
    else:
        logger.info(
            f"📥 RQ: Enqueued conversation complete event job {event_dispatch_job.id}, meta={event_dispatch_job.meta} (no dependencies, starts immediately)"
        )

    # Notify frontend that post-conversation pipeline is queued
    publish_sse_event(
        user_id,
        "jobs.queued",
        {
            "conversation_id": conversation_id,
            "client_id": client_id or "",
            "jobs": [
                j
                for j in [
                    "speaker_recognition" if speaker_job else None,
                    "memory_extraction" if memory_job else None,
                    "title_summary",
                    "event_dispatch",
                ]
                if j
            ],
        },
    )

    return {
        "speaker_recognition": speaker_job.id if speaker_job else None,
        "memory": memory_job.id if memory_job else None,
        "title_summary": title_summary_job.id,
        "event_dispatch": event_dispatch_job.id,
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
        "worker_fleet": {
            "healthy": False,
            "status": "unknown",
            "detail": "Worker fleet health has not been checked",
        },
    }

    # Check Redis connection
    try:
        redis_conn.ping()
        health["redis_connection"] = "healthy"
        health["worker_fleet"] = evaluate_fleet_health(redis_conn.get(FLEET_HEALTH_KEY))
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
    workers = [
        worker
        for worker in Worker.all(connection=redis_conn)
        if is_rq_worker_fresh(worker)
    ]
    health["total_workers"] = len(workers)

    for worker in workers:
        state = worker.get_state()
        current_job = worker.get_current_job_id()

        # Count active vs idle workers
        if current_job or state == "busy":
            health["active_workers"] += 1
        else:
            health["idle_workers"] += 1

        health["workers"].append(
            {
                "name": worker.name,
                "state": state,
                "queues": [q.name for q in worker.queues],
                "current_job": current_job,
            }
        )

    return health


# needs tidying but works for now
async def cleanup_stuck_stream_workers(request):
    """Clean up stuck Redis Stream consumers and pending messages from all active streams."""
    try:
        # Get Redis client from request.app.state (initialized during startup)
        redis_client = request.app.state.redis_audio_stream

        if not redis_client:
            return JSONResponse(
                status_code=503,
                content={"error": "Redis client for audio streaming not initialized"},
            )

        cleanup_results = {}
        total_cleaned = 0
        total_deleted_consumers = 0
        total_deleted_streams = 0
        current_time = time.time()

        # Discover all audio streams (per-client streams)
        stream_keys = await redis_client.keys("audio:stream:*")

        for stream_key in stream_keys:
            stream_name = (
                stream_key.decode() if isinstance(stream_key, bytes) else stream_key
            )

            try:
                # First check stream age - delete old streams (>1 hour) immediately
                stream_info = await redis_client.execute_command(
                    "XINFO", "STREAM", stream_name
                )

                # Parse stream info
                info_dict = {}
                for i in range(0, len(stream_info), 2):
                    key_name = (
                        stream_info[i].decode()
                        if isinstance(stream_info[i], bytes)
                        else str(stream_info[i])
                    )
                    info_dict[key_name] = stream_info[i + 1]

                stream_length = int(info_dict.get("length", 0))
                last_entry = info_dict.get("last-entry")

                # Check if stream is old
                should_delete_stream = False
                stream_age = 0

                if stream_length == 0:
                    should_delete_stream = True
                    stream_age = 0
                elif (
                    last_entry and isinstance(last_entry, list) and len(last_entry) > 0
                ):
                    try:
                        last_id = last_entry[0]
                        if isinstance(last_id, bytes):
                            last_id = last_id.decode()
                        last_timestamp_ms = int(last_id.split("-")[0])
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
                        "stream_age": stream_age,
                    }
                    continue

                # Get consumer groups
                groups = await redis_client.execute_command(
                    "XINFO", "GROUPS", stream_name
                )

                if not groups:
                    cleanup_results[stream_name] = {
                        "message": "No consumer groups found",
                        "cleaned": 0,
                        "deleted_stream": False,
                    }
                    continue

                # Parse first group
                group_dict = {}
                group = groups[0]
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
                            value = str(value)
                    group_dict[key] = value

                group_name = group_dict.get("name", "unknown")
                if isinstance(group_name, bytes):
                    group_name = group_name.decode()

                pending_count = int(group_dict.get("pending", 0))

                # Get consumers for this group to check per-consumer pending
                consumers = await redis_client.execute_command(
                    "XINFO", "CONSUMERS", stream_name, group_name
                )

                cleaned_count = 0
                total_consumer_pending = 0

                # Clean up pending messages for each consumer AND delete dead consumers
                deleted_consumers = 0
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
                        logger.info(
                            f"Found {consumer_pending} pending messages for consumer {consumer_name} (idle: {consumer_idle_ms}ms)"
                        )

                        # Get pending messages for this specific consumer
                        try:
                            pending_messages = await redis_client.execute_command(
                                "XPENDING",
                                stream_name,
                                group_name,
                                "-",
                                "+",
                                str(consumer_pending),
                                consumer_name,
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
                                            "XCLAIM",
                                            stream_name,
                                            group_name,
                                            "cleanup-worker",
                                            "0",
                                            msg_id,
                                        )

                                        # Acknowledge it immediately
                                        await redis_client.xack(
                                            stream_name, group_name, msg_id
                                        )
                                        cleaned_count += 1
                                    except Exception as claim_error:
                                        logger.warning(
                                            f"Failed to claim/ack message {msg_id}: {claim_error}"
                                        )

                        except Exception as consumer_error:
                            logger.error(
                                f"Error processing consumer {consumer_name}: {consumer_error}"
                            )

                    # Delete dead consumers (idle > 5 minutes with no pending messages)
                    if is_dead and consumer_pending == 0:
                        try:
                            await redis_client.execute_command(
                                "XGROUP",
                                "DELCONSUMER",
                                stream_name,
                                group_name,
                                consumer_name,
                            )
                            deleted_consumers += 1
                            logger.info(
                                f"🧹 Deleted dead consumer {consumer_name} (idle: {consumer_idle_ms}ms)"
                            )
                        except Exception as delete_error:
                            logger.warning(
                                f"Failed to delete consumer {consumer_name}: {delete_error}"
                            )

                if total_consumer_pending == 0 and deleted_consumers == 0:
                    cleanup_results[stream_name] = {
                        "message": "No pending messages or dead consumers",
                        "cleaned": 0,
                        "deleted_consumers": 0,
                        "deleted_stream": False,
                    }
                    continue

                total_cleaned += cleaned_count
                total_deleted_consumers += deleted_consumers
                cleanup_results[stream_name] = {
                    "message": f"Cleaned {cleaned_count} pending messages, deleted {deleted_consumers} dead consumers",
                    "cleaned": cleaned_count,
                    "deleted_consumers": deleted_consumers,
                    "deleted_stream": False,
                    "original_pending": pending_count,
                }

            except Exception as e:
                cleanup_results[stream_name] = {"error": str(e), "cleaned": 0}

        return {
            "success": True,
            "total_cleaned": total_cleaned,
            "total_deleted_consumers": total_deleted_consumers,
            "total_deleted_streams": total_deleted_streams,
            "streams": cleanup_results,  # New key for per-stream results
            "providers": cleanup_results,  # Keep for backward compatibility with frontend
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"Error cleaning up stuck workers: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to cleanup stuck workers: {str(e)}"},
        )
