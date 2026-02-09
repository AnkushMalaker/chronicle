"""
Simple queue API routes for job monitoring.
Provides basic endpoints for viewing job status and statistics.
"""

import logging
from typing import List, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from rq.job import Job

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.controllers.queue_controller import (
    QUEUE_NAMES,
    get_job_stats,
    get_job_status_from_rq,
    get_jobs,
    redis_conn,
)
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("/jobs")
async def list_jobs(
    limit: int = Query(20, ge=1, le=100, description="Number of jobs to return"),
    offset: int = Query(0, ge=0, description="Number of jobs to skip"),
    queue_name: str = Query(None, description="Filter by queue name"),
    job_type: str = Query(None, description="Filter by job type (matches func_name)"),
    client_id: str = Query(None, description="Filter by client_id in meta"),
    current_user: User = Depends(current_active_user)
):
    """List jobs with pagination and filtering."""
    try:
        result = get_jobs(limit=limit, offset=offset, queue_name=queue_name, job_type=job_type, client_id=client_id)

        # Filter jobs by user if not admin
        if not current_user.is_superuser:
            # Filter based on user_id in job kwargs (where RQ stores job parameters)
            user_jobs = []
            for job in result["jobs"]:
                job_kwargs = job.get("kwargs", {})
                if job_kwargs.get("user_id") == str(current_user.user_id):
                    user_jobs.append(job)

            result["jobs"] = user_jobs
            result["pagination"]["total"] = len(user_jobs)

        return result

    except Exception as e:
        logger.error(f"Failed to list jobs: {e}")
        return {"error": "Failed to list jobs", "jobs": [], "pagination": {"total": 0, "limit": limit, "offset": offset, "has_more": False}}


@router.get("/jobs/{job_id}/status")
async def get_job_status(
    job_id: str,
    current_user: User = Depends(current_active_user)
):
    """Get just the status of a specific job (lightweight endpoint)."""
    try:
        job = Job.fetch(job_id, connection=redis_conn)

        # Check user permission (non-admins can only see their own jobs)
        if not current_user.is_superuser:
            job_user_id = job.kwargs.get("user_id") if job.kwargs else None
            if job_user_id != str(current_user.user_id):
                raise HTTPException(status_code=403, detail="Access forbidden")

        # Get status using RQ's native method
        try:
            status = get_job_status_from_rq(job)
        except RuntimeError as e:
            logger.error(f"Failed to determine status for job {job_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        response = {
            "job_id": job.id,
            "status": status
        }

        # Include error information for failed jobs
        if status == "failed" and job.exc_info:
            response["error_message"] = str(job.exc_info)
            response["exc_info"] = str(job.exc_info)

        return response

    except HTTPException:
        # Re-raise HTTPException unchanged (e.g., 403 Forbidden)
        raise
    except Exception as e:
        logger.error(f"Failed to get job status {job_id}: {e}")
        raise HTTPException(status_code=404, detail="Job not found")


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    current_user: User = Depends(current_active_user)
):
    """Get detailed job information including result."""
    try:
        job = Job.fetch(job_id, connection=redis_conn)

        # Check user permission (non-admins can only see their own jobs)
        if not current_user.is_superuser:
            job_user_id = job.kwargs.get("user_id") if job.kwargs else None
            if job_user_id != str(current_user.user_id):
                raise HTTPException(status_code=403, detail="Access forbidden")

        # Get status using RQ's native method
        try:
            status = get_job_status_from_rq(job)
        except RuntimeError as e:
            logger.error(f"Failed to determine status for job {job_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        return {
            "job_id": job.id,
            "status": status,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "ended_at": job.ended_at.isoformat() if job.ended_at else None,
            "description": job.description or "",
            "func_name": job.func_name if hasattr(job, 'func_name') else "",
            "args": job.args,
            "kwargs": job.kwargs,
            "meta": job.meta if job.meta else {},
            "result": job.result,
            "error_message": str(job.exc_info) if job.exc_info else None,
        }

    except HTTPException:
        # Re-raise HTTPException unchanged (e.g., 403 Forbidden)
        raise
    except Exception as e:
        logger.error(f"Failed to get job {job_id}: {e}")
        raise HTTPException(status_code=404, detail="Job not found")


@router.delete("/jobs/{job_id}")
async def cancel_job(
    job_id: str,
    current_user: User = Depends(current_active_user)
):
    """Cancel or delete a job."""
    try:
        job = Job.fetch(job_id, connection=redis_conn)

        # Check user permission (non-admins can only cancel their own jobs)
        if not current_user.is_superuser:
            job_user_id = job.kwargs.get("user_id") if job.kwargs else None
            if job_user_id != str(current_user.user_id):
                raise HTTPException(status_code=403, detail="Access forbidden")

        # Cancel if queued or started, delete if finished/failed
        if job.is_queued or job.is_started or job.is_deferred or job.is_scheduled:
            # Cancel the job
            job.cancel()
            logger.info(f"Cancelled job {job_id}")
            return {
                "job_id": job_id,
                "action": "canceled",
                "message": f"Job {job_id} has been canceled"
            }
        else:
            # Delete finished/failed jobs
            job.delete()
            logger.info(f"Deleted job {job_id}")
            return {
                "job_id": job_id,
                "action": "deleted",
                "message": f"Job {job_id} has been deleted"
            }

    except HTTPException:
        # Re-raise HTTPException unchanged (e.g., 403 Forbidden)
        raise
    except Exception as e:
        logger.error(f"Failed to cancel/delete job {job_id}: {e}")
        raise HTTPException(status_code=404, detail=f"Job not found or could not be canceled: {str(e)}")


@router.get("/jobs/by-client/{client_id}")
async def get_jobs_by_client(
    client_id: str,
    current_user: User = Depends(current_active_user)
):
    """Get all jobs associated with a specific client device."""
    try:
        from rq.registry import (
            CanceledJobRegistry,
            DeferredJobRegistry,
            FailedJobRegistry,
            FinishedJobRegistry,
            ScheduledJobRegistry,
            StartedJobRegistry,
        )

        from advanced_omi_backend.controllers.queue_controller import get_queue
        from advanced_omi_backend.models.conversation import Conversation

        all_jobs = []
        processed_job_ids = set()  # Track which jobs we've already processed
        queues = QUEUE_NAMES

        def get_job_status(job, registries_map):
            """Determine job status using RQ's native method."""
            try:
                return get_job_status_from_rq(job)
            except RuntimeError:
                # In nested function, can't raise HTTP exception
                # Log and re-raise to be handled by outer scope
                logger.error(f"Job {job.id} status determination failed")
                raise

        def process_job_and_dependents(job, queue_name, base_status):
            """Process a job and recursively find all its dependents."""
            if job.id in processed_job_ids:
                return

            processed_job_ids.add(job.id)

            # Check user permission (non-admins can only see their own jobs)
            if not current_user.is_superuser:
                job_user_id = job.kwargs.get("user_id") if job.kwargs else None
                if job_user_id != str(current_user.user_id):
                    return

            # Get accurate status
            status = get_job_status(job, {})

            # Add this job to results
            all_jobs.append({
                "job_id": job.id,
                "job_type": job.func_name.split('.')[-1] if job.func_name else "unknown",
                "queue": queue_name,
                "status": status,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "ended_at": job.ended_at.isoformat() if job.ended_at else None,
                "description": job.description or "",
                "result": job.result,
                "meta": job.meta if job.meta else {},
                "args": job.args,
                "kwargs": job.kwargs if job.kwargs else {},
                "error_message": str(job.exc_info) if job.exc_info else None,
            })

            # Check for dependent jobs (jobs that depend on this one)
            try:
                dependent_ids = job.dependent_ids
                if dependent_ids:
                    logger.debug(f"Job {job.id} has {len(dependent_ids)} dependents: {dependent_ids}")

                    for dep_id in dependent_ids:
                        try:
                            dep_job = Job.fetch(dep_id, connection=redis_conn)
                            # Recursively process dependent job
                            process_job_and_dependents(dep_job, queue_name, "waiting")
                        except Exception as e:
                            logger.debug(f"Error fetching dependent job {dep_id}: {e}")
            except Exception as e:
                logger.debug(f"Error checking dependents for job {job.id}: {e}")

        # Find all jobs that match the session
        for queue_name in queues:
            queue = get_queue(queue_name)

            # Check all registries (using RQ standard status names)
            registries = [
                ("queued", queue.job_ids),
                ("started", StartedJobRegistry(queue=queue).get_job_ids()),  # RQ standard
                ("finished", FinishedJobRegistry(queue=queue).get_job_ids()),  # RQ standard
                ("failed", FailedJobRegistry(queue=queue).get_job_ids()),
                ("canceled", CanceledJobRegistry(queue=queue).get_job_ids()),  # RQ standard (US spelling)
                ("deferred", DeferredJobRegistry(queue=queue).get_job_ids()),
                ("scheduled", ScheduledJobRegistry(queue=queue).get_job_ids())
            ]

            for status_name, job_ids in registries:
                for job_id in job_ids:
                    try:
                        job = Job.fetch(job_id, connection=redis_conn)

                        # Check if this job belongs to the requested client
                        matches_client = False

                        # Check job.meta for client_id (current standard)
                        if job.meta and 'client_id' in job.meta:
                            if job.meta['client_id'] == client_id:
                                matches_client = True

                        if matches_client:
                            # Process this job and all its dependents
                            process_job_and_dependents(job, queue_name, status_name)

                    except Exception as e:
                        logger.debug(f"Error fetching job {job_id}: {e}")
                        continue

        # Sort by created_at
        all_jobs.sort(key=lambda x: x["created_at"] or "", reverse=False)

        logger.info(f"Found {len(all_jobs)} jobs for client {client_id} (including dependents)")

        return {
            "client_id": client_id,
            "jobs": all_jobs,
            "total": len(all_jobs)
        }

    except Exception as e:
        logger.error(f"Failed to get jobs for client {client_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get jobs for client: {str(e)}")


@router.get("/events")
async def get_events(
    limit: int = Query(50, ge=1, le=200, description="Number of recent events"),
    event_type: str = Query(None, description="Filter by event type"),
    current_user: User = Depends(current_active_user),
):
    """Get recent system events from the event log (admin only)."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        from advanced_omi_backend.services.plugin_service import get_plugin_router

        router_instance = get_plugin_router()
        if not router_instance:
            return {"events": [], "total": 0}

        events = router_instance.get_recent_events(limit=limit, event_type=event_type or None)
        return {"events": events, "total": len(events)}
    except Exception as e:
        logger.error(f"Failed to get events: {e}")
        return {"events": [], "total": 0}


@router.get("/stats")
async def get_queue_stats_endpoint(
    current_user: User = Depends(current_active_user)
):
    """Get queue statistics."""
    try:
        stats = get_job_stats()
        return stats

    except Exception as e:
        logger.error(f"Failed to get queue stats: {e}")
        return {"total_jobs": 0, "queued_jobs": 0, "started_jobs": 0, "finished_jobs": 0, "failed_jobs": 0, "canceled_jobs": 0, "deferred_jobs": 0}


@router.get("/worker-details")
async def get_queue_worker_details(
    current_user: User = Depends(current_active_user)
):
    """Get detailed queue and worker status including task manager health."""
    try:
        import time

        from advanced_omi_backend.controllers.queue_controller import get_queue_health

        # Get queue health directly
        queue_health = get_queue_health()

        status = {
            "architecture": "rq_workers",
            "timestamp": int(time.time()),
            "workers": {
                "total": queue_health.get("total_workers", 0),
                "active": queue_health.get("active_workers", 0),
                "idle": queue_health.get("idle_workers", 0),
                "details": queue_health.get("workers", [])
            },
            "queues": queue_health.get("queues", {}),
            "redis_connection": queue_health.get("redis_connection", "unknown")
        }

        return status

    except Exception as e:
        logger.error(f"Failed to get queue worker details: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get worker details: {str(e)}")


@router.get("/streams")
async def get_stream_stats(
    limit: int = Query(default=10, ge=1, le=100),  # Max 100 streams to prevent timeouts
    current_user: User = Depends(current_active_user)
):
    """Get Redis Streams statistics with consumer group information."""
    try:
        from advanced_omi_backend.services.audio_service import get_audio_stream_service
        audio_service = get_audio_stream_service()

        if not audio_service.redis:
            return {
                "error": "Audio stream service not connected",
                "streams": []
            }

        # Get audio streams with limit
        stream_keys = []
        cursor = b"0"
        while cursor and len(stream_keys) < limit:
            cursor, keys = await audio_service.redis.scan(
                cursor, match=f"{audio_service.audio_stream_prefix}*", count=limit
            )
            stream_keys.extend(keys[:limit - len(stream_keys)])

        # Use asyncio.gather to fetch stream info in parallel
        import asyncio

        async def get_stream_info(stream_key):
            try:
                stream_name = stream_key.decode() if isinstance(stream_key, bytes) else stream_key

                # Get basic stream info
                info = await audio_service.redis.xinfo_stream(stream_name)

                # Get consumer groups info
                groups_info = []
                try:
                    groups = await audio_service.redis.xinfo_groups(stream_name)
                    for group in groups:
                        group_dict = {}
                        # Parse group info (alternating key-value pairs)
                        for i in range(0, len(group), 2):
                            if i+1 < len(group):
                                key = group[i].decode() if isinstance(group[i], bytes) else str(group[i])
                                value = group[i+1]
                                if isinstance(value, bytes):
                                    try:
                                        value = value.decode()
                                    except:
                                        value = str(value)
                                group_dict[key] = value

                        # Get consumers for this group
                        consumers = []
                        try:
                            consumers_raw = await audio_service.redis.xinfo_consumers(stream_name, group_dict.get('name', ''))
                            for consumer in consumers_raw:
                                consumer_dict = {}
                                for i in range(0, len(consumer), 2):
                                    if i+1 < len(consumer):
                                        key = consumer[i].decode() if isinstance(consumer[i], bytes) else str(consumer[i])
                                        value = consumer[i+1]
                                        if isinstance(value, bytes):
                                            try:
                                                value = value.decode()
                                            except:
                                                value = str(value)
                                        consumer_dict[key] = value
                                consumers.append(consumer_dict)
                        except Exception as ce:
                            logger.debug(f"Could not fetch consumers for group {group_dict.get('name')}: {ce}")

                        groups_info.append({
                            "name": group_dict.get('name', 'unknown'),
                            "consumers": group_dict.get('consumers', 0),
                            "pending": group_dict.get('pending', 0),
                            "last_delivered_id": group_dict.get('last-delivered-id', 'N/A'),
                            "consumer_details": consumers
                        })
                except Exception as ge:
                    logger.debug(f"No consumer groups for stream {stream_name}: {ge}")

                return {
                    "stream_name": stream_name,
                    "length": info[b"length"],
                    "first_entry_id": info[b"first-entry"][0].decode() if info[b"first-entry"] else None,
                    "last_entry_id": info[b"last-entry"][0].decode() if info[b"last-entry"] else None,
                    "groups": groups_info
                }
            except Exception as e:
                logger.error(f"Error getting info for stream {stream_key}: {e}")
                return None

        # Fetch all stream info in parallel
        streams_info_results = await asyncio.gather(*[get_stream_info(key) for key in stream_keys])
        streams_info = [info for info in streams_info_results if info is not None]

        return {
            "total_streams": len(streams_info),
            "streams": streams_info,
            "limited": len(stream_keys) >= limit
        }

    except Exception as e:
        logger.error(f"Failed to get stream stats: {e}", exc_info=True)
        return {
            "error": str(e),
            "total_streams": 0,
            "streams": []
        }


class FlushJobsRequest(BaseModel):
    older_than_hours: int = 24
    statuses: List[str] = ["finished", "failed", "canceled"]  # RQ standard status names


class FlushAllJobsRequest(BaseModel):
    confirm: bool
    include_failed: bool = False  # By default, preserve failed jobs for debugging
    include_finished: bool = False  # By default, preserve finished jobs for debugging


@router.post("/flush")
async def flush_jobs(
    request: FlushJobsRequest,
    current_user: User = Depends(current_active_user)
):
    """Flush old inactive jobs based on age and status."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        from datetime import datetime, timedelta, timezone

        from rq.registry import (
            CanceledJobRegistry,
            FailedJobRegistry,
            FinishedJobRegistry,
        )

        from advanced_omi_backend.controllers.queue_controller import get_queue

        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=request.older_than_hours)
        total_removed = 0

        # Get all queues
        queues = QUEUE_NAMES

        for queue_name in queues:
            queue = get_queue(queue_name)

            # Flush from appropriate registries based on requested statuses (RQ standard names)
            if "finished" in request.statuses:  # RQ standard, not "completed"
                registry = FinishedJobRegistry(queue=queue)
                for job_id in registry.get_job_ids():
                    try:
                        job = Job.fetch(job_id, connection=redis_conn)
                        if job.ended_at and job.ended_at < cutoff_time:
                            job.delete()
                            total_removed += 1
                    except Exception as e:
                        logger.error(f"Error deleting job {job_id}: {e}")

            if "failed" in request.statuses:
                registry = FailedJobRegistry(queue=queue)
                for job_id in registry.get_job_ids():
                    try:
                        job = Job.fetch(job_id, connection=redis_conn)
                        if job.ended_at and job.ended_at < cutoff_time:
                            job.delete()
                            total_removed += 1
                    except Exception as e:
                        logger.error(f"Error deleting job {job_id}: {e}")

            if "canceled" in request.statuses:  # RQ standard (US spelling), not "cancelled"
                registry = CanceledJobRegistry(queue=queue)
                for job_id in registry.get_job_ids():
                    try:
                        job = Job.fetch(job_id, connection=redis_conn)
                        if job.ended_at and job.ended_at < cutoff_time:
                            job.delete()
                            total_removed += 1
                    except Exception as e:
                        logger.error(f"Error deleting job {job_id}: {e}")

        return {
            "total_removed": total_removed,
            "cutoff_time": cutoff_time.isoformat(),
            "statuses": request.statuses
        }

    except Exception as e:
        logger.error(f"Failed to flush jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to flush jobs: {str(e)}")


@router.post("/flush-all")
async def flush_all_jobs(
    request: FlushAllJobsRequest,
    current_user: User = Depends(current_active_user)
):
    """
    Flush jobs from queues and registries.
    By default preserves failed and finished jobs for debugging.
    Set include_failed=true or include_finished=true to flush those as well.
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    if not request.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")

    try:
        from rq.registry import (
            CanceledJobRegistry,
            DeferredJobRegistry,
            FailedJobRegistry,
            FinishedJobRegistry,
            ScheduledJobRegistry,
            StartedJobRegistry,
        )

        from advanced_omi_backend.controllers.queue_controller import get_queue

        total_removed = 0
        queues = QUEUE_NAMES

        for queue_name in queues:
            queue = get_queue(queue_name)

            # First, empty the queue itself (removes queued jobs)
            queued_count = len(queue)
            queue.empty()
            total_removed += queued_count
            logger.info(f"Emptied {queued_count} queued jobs from {queue_name}")

            # Build list of registries to flush based on request parameters
            registries = [
                ("started", StartedJobRegistry(queue=queue)),  # Always flush in-progress
                ("deferred", DeferredJobRegistry(queue=queue)),  # Always flush deferred
                ("scheduled", ScheduledJobRegistry(queue=queue)),  # Always flush scheduled
                ("canceled", CanceledJobRegistry(queue=queue))  # Always flush canceled
            ]

            # Conditionally add failed and finished registries
            if request.include_failed:
                registries.append(("failed", FailedJobRegistry(queue=queue)))
            if request.include_finished:
                registries.append(("finished", FinishedJobRegistry(queue=queue)))

            for registry_name, registry in registries:
                job_ids = list(registry.get_job_ids())  # Convert to list to avoid iterator issues
                logger.info(f"Flushing {len(job_ids)} jobs from {queue_name}/{registry_name}")

                for job_id in job_ids:
                    try:
                        # Try to fetch the job
                        job = Job.fetch(job_id, connection=redis_conn)

                        # Skip session-level jobs (e.g., speech_detection, audio_persistence)
                        # These run for the entire session and should not be killed by test cleanup
                        if job.meta and job.meta.get("session_level"):
                            logger.info(f"Skipping session-level job {job_id} ({job.description})")
                            continue

                        # Handle running jobs differently to avoid worker deadlock
                        if job.is_started:
                            # Send stop command to worker instead of canceling/deleting immediately
                            # This lets the worker clean up gracefully and prevents deadlock
                            try:
                                from rq.command import send_stop_job_command
                                send_stop_job_command(redis_conn, job_id)
                                logger.info(f"Sent stop command to worker for job {job_id}")
                                # Don't delete yet - let worker move it to canceled/failed registry
                                # It will be cleaned up on next flush or by worker cleanup
                                continue
                            except Exception as stop_error:
                                logger.warning(f"Could not send stop command to job {job_id}: {stop_error}")
                                # If stop fails, try to cancel it (may already be finishing)
                                try:
                                    job.cancel()
                                    logger.info(f"Cancelled job {job_id} after stop failed")
                                except Exception as cancel_error:
                                    logger.warning(f"Could not cancel job {job_id}: {cancel_error}")

                        # For non-running jobs, safe to delete immediately
                        job.delete()
                        total_removed += 1

                    except Exception as e:
                        # Job might already be deleted or not exist - try to remove from registry anyway
                        logger.warning(f"Error deleting job {job_id}: {e}")
                        try:
                            registry.remove(job_id)
                            logger.info(f"Removed stale job reference {job_id} from {registry_name} registry")
                        except Exception as reg_error:
                            logger.error(f"Could not remove {job_id} from registry: {reg_error}")

        # Also clean up audio streams and consumer locks
        deleted_keys = 0

        # Get async Redis connection for scanning
        from advanced_omi_backend.controllers.queue_controller import REDIS_URL
        async_redis = await aioredis.from_url(REDIS_URL)

        try:
            # Delete audio streams
            cursor = 0
            while True:
                cursor, keys = await async_redis.scan(cursor, match="audio:*", count=1000)
                if keys:
                    await async_redis.delete(*keys)
                    deleted_keys += len(keys)
                if cursor == 0:
                    break

            # Delete consumer locks
            cursor = 0
            while True:
                cursor, keys = await async_redis.scan(cursor, match="consumer:*", count=1000)
                if keys:
                    await async_redis.delete(*keys)
                    deleted_keys += len(keys)
                if cursor == 0:
                    break
        finally:
            await async_redis.close()

        preserved = []
        if not request.include_failed:
            preserved.append("failed jobs")
        if not request.include_finished:
            preserved.append("finished jobs")

        preserved_msg = f" (preserved {', '.join(preserved)})" if preserved else ""
        logger.info(f"Flushed {total_removed} jobs and {deleted_keys} Redis keys from all queues{preserved_msg}")

        return {
            "total_removed": total_removed,
            "deleted_keys": deleted_keys,
            "preserved": preserved,
            "message": f"Flushed {total_removed} jobs{preserved_msg}"
        }

    except Exception as e:
        logger.error(f"Failed to flush all jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to flush all jobs: {str(e)}")


@router.get("/sessions")
async def get_redis_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(current_active_user)
):
    """Get Redis session tracking information."""
    try:
        import redis.asyncio as aioredis

        from advanced_omi_backend.controllers.queue_controller import REDIS_URL

        redis_client = aioredis.from_url(REDIS_URL)

        # Get session keys
        session_keys = []
        cursor = b"0"
        while cursor and len(session_keys) < limit:
            cursor, keys = await redis_client.scan(
                cursor, match="audio:session:*", count=limit
            )
            session_keys.extend(keys[:limit - len(session_keys)])

        # Get session info
        sessions = []
        for key in session_keys:
            try:
                session_data = await redis_client.hgetall(key)
                if session_data:
                    session_id = key.decode().replace("audio:session:", "")

                    # Get conversation count for this session
                    conversation_count_key = f"session:conversation_count:{session_id}"
                    conversation_count_bytes = await redis_client.get(conversation_count_key)
                    conversation_count = int(conversation_count_bytes.decode()) if conversation_count_bytes else 0

                    sessions.append({
                        "session_id": session_id,
                        "user_id": session_data.get(b"user_id", b"").decode(),
                        "client_id": session_data.get(b"client_id", b"").decode(),
                        "stream_name": session_data.get(b"stream_name", b"").decode(),
                        "provider": session_data.get(b"provider", b"").decode(),
                        "mode": session_data.get(b"mode", b"").decode(),
                        "status": session_data.get(b"status", b"").decode(),
                        "started_at": session_data.get(b"started_at", b"").decode(),
                        "chunks_published": int(session_data.get(b"chunks_published", b"0").decode() or 0),
                        "last_chunk_at": session_data.get(b"last_chunk_at", b"").decode(),
                        "conversation_count": conversation_count
                    })
            except Exception as e:
                logger.error(f"Error getting session info for {key}: {e}")

        await redis_client.close()

        return {
            "total_sessions": len(sessions),
            "sessions": sessions
        }

    except Exception as e:
        logger.error(f"Failed to get sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get sessions: {str(e)}")


@router.post("/sessions/clear")
async def clear_old_sessions(
    older_than_seconds: int = Query(default=3600, description="Clear sessions older than N seconds"),
    current_user: User = Depends(current_active_user)
):
    """Clear old Redis sessions that are stuck or inactive."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        import time

        import redis.asyncio as aioredis

        from advanced_omi_backend.controllers.queue_controller import REDIS_URL

        redis_client = aioredis.from_url(REDIS_URL)
        current_time = time.time()
        cutoff_time = current_time - older_than_seconds

        # Get all session keys
        session_keys = []
        cursor = b"0"
        while cursor:
            cursor, keys = await redis_client.scan(cursor, match="audio:session:*", count=100)
            session_keys.extend(keys)

        # Check each session and delete if old
        deleted_count = 0
        for key in session_keys:
            try:
                session_data = await redis_client.hgetall(key)
                if session_data:
                    last_chunk_at = session_data.get(b"last_chunk_at", b"").decode()
                    if last_chunk_at:
                        last_chunk_time = float(last_chunk_at)
                        if last_chunk_time < cutoff_time:
                            await redis_client.delete(key)
                            deleted_count += 1
                            logger.info(f"Deleted old session: {key.decode()}")
            except Exception as e:
                logger.error(f"Error processing session {key}: {e}")

        await redis_client.close()

        return {
            "deleted_count": deleted_count,
            "cutoff_seconds": older_than_seconds
        }

    except Exception as e:
        logger.error(f"Failed to clear sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to clear sessions: {str(e)}")


@router.get("/dashboard")
async def get_dashboard_data(
    request: Request,
    expanded_clients: str = Query(default="", description="Comma-separated list of client IDs to fetch jobs for"),
    current_user: User = Depends(current_active_user)
):
    """Get all data needed for the Queue dashboard in a single API call.

    Returns:
    - Jobs grouped by status (queued, started, finished, failed)
    - Queue statistics
    - Streaming status
    - Client jobs for expanded clients
    """
    try:
        from rq.registry import (
            FailedJobRegistry,
            FinishedJobRegistry,
            StartedJobRegistry,
        )

        from advanced_omi_backend.controllers import system_controller
        from advanced_omi_backend.controllers.queue_controller import get_queue

        # Parse expanded clients list
        expanded_client_ids = [c.strip() for c in expanded_clients.split(",") if c.strip()] if expanded_clients else []

        # Fetch all data in parallel
        import asyncio

        async def fetch_jobs_by_status(status_name: str, limit: int = 100):
            """Fetch jobs by status using existing registry logic."""
            try:
                queues = QUEUE_NAMES
                all_jobs = []

                for queue_name in queues:
                    queue = get_queue(queue_name)

                    # Get job IDs based on status (using RQ standard status names)
                    if status_name == "queued":
                        job_ids = queue.job_ids[:limit]
                    elif status_name == "started":  # RQ standard, not "processing"
                        job_ids = list(StartedJobRegistry(queue=queue).get_job_ids())[:limit]
                    elif status_name == "finished":  # RQ standard, not "completed"
                        job_ids = list(FinishedJobRegistry(queue=queue).get_job_ids())[:limit]
                    elif status_name == "failed":
                        job_ids = list(FailedJobRegistry(queue=queue).get_job_ids())[:limit]
                    else:
                        continue

                    # Fetch job details
                    for job_id in job_ids:
                        try:
                            job = Job.fetch(job_id, connection=redis_conn)

                            # Check user permission
                            if not current_user.is_superuser:
                                job_user_id = job.kwargs.get("user_id") if job.kwargs else None
                                if job_user_id != str(current_user.user_id):
                                    continue

                            # Add job with metadata
                            all_jobs.append({
                                "job_id": job.id,
                                "job_type": job.func_name.split('.')[-1] if job.func_name else "unknown",
                                "user_id": job.kwargs.get("user_id") if job.kwargs else None,
                                "status": status_name,
                                "priority": "normal",  # RQ doesn't have priority concept
                                "data": {"description": job.description or ""},
                                "result": job.result,
                                "meta": job.meta if job.meta else {},
                                "kwargs": job.kwargs if job.kwargs else {},
                                "error_message": str(job.exc_info) if job.exc_info else None,
                                "created_at": job.created_at.isoformat() if job.created_at else None,
                                "started_at": job.started_at.isoformat() if job.started_at else None,
                                "ended_at": job.ended_at.isoformat() if job.ended_at else None,
                                "retry_count": 0,  # RQ doesn't track this by default
                                "max_retries": 0,
                                "progress_percent": 0,
                                "progress_message": "",
                                "queue": queue_name
                            })
                        except Exception as e:
                            logger.debug(f"Error fetching job {job_id}: {e}")
                            continue

                return all_jobs
            except Exception as e:
                logger.error(f"Error fetching {status_name} jobs: {e}")
                return []

        async def fetch_stats():
            """Fetch queue stats."""
            try:
                return get_job_stats()
            except Exception as e:
                logger.error(f"Error fetching stats: {e}")
                return {"total_jobs": 0, "queued_jobs": 0, "started_jobs": 0, "finished_jobs": 0, "failed_jobs": 0}

        async def fetch_streaming_status():
            """Fetch streaming status."""
            try:
                # Import session_controller for streaming status
                from advanced_omi_backend.controllers import session_controller

                # Use the actual request object from the parent function
                return await session_controller.get_streaming_status(request)
            except Exception as e:
                logger.error(f"Error fetching streaming status: {e}")
                return {"active_sessions": [], "stream_health": {}, "rq_queues": {}}

        async def fetch_client_jobs(client_id: str):
            """Fetch jobs for a specific client device."""
            try:
                # Reuse the existing logic from get_jobs_by_client endpoint
                from advanced_omi_backend.models.conversation import Conversation

                all_jobs = []
                processed_job_ids = set()
                queues = QUEUE_NAMES

                def get_job_status(job):
                    """Get job status using RQ's native method."""
                    try:
                        return get_job_status_from_rq(job)
                    except RuntimeError:
                        logger.error(f"Job {job.id} status determination failed")
                        # Return unknown as fallback in dashboard context
                        return "unknown"

                # Find all jobs for this session
                for queue_name in queues:
                    queue = get_queue(queue_name)

                    # Check all registries
                    from rq.registry import (
                        CanceledJobRegistry,
                        DeferredJobRegistry,
                        FailedJobRegistry,
                        FinishedJobRegistry,
                        ScheduledJobRegistry,
                        StartedJobRegistry,
                    )

                    registries = [
                        ("queued", queue.job_ids),
                        ("started", StartedJobRegistry(queue=queue).get_job_ids()),  # RQ standard
                        ("finished", FinishedJobRegistry(queue=queue).get_job_ids()),  # RQ standard
                        ("failed", FailedJobRegistry(queue=queue).get_job_ids())
                    ]

                    for status_name, job_ids in registries:
                        for job_id in job_ids:
                            if job_id in processed_job_ids:
                                continue

                            try:
                                job = Job.fetch(job_id, connection=redis_conn)

                                # Check if job belongs to this client
                                matches_client = False
                                if job.meta and 'client_id' in job.meta and job.meta['client_id'] == client_id:
                                    matches_client = True

                                if not matches_client:
                                    continue

                                # Check user permission
                                if not current_user.is_superuser:
                                    job_user_id = job.kwargs.get("user_id") if job.kwargs else None
                                    if job_user_id != str(current_user.user_id):
                                        continue

                                processed_job_ids.add(job_id)
                                all_jobs.append({
                                    "job_id": job.id,
                                    "job_type": job.func_name.split('.')[-1] if job.func_name else "unknown",
                                    "queue": queue_name,
                                    "status": get_job_status(job),
                                    "created_at": job.created_at.isoformat() if job.created_at else None,
                                    "started_at": job.started_at.isoformat() if job.started_at else None,
                                    "ended_at": job.ended_at.isoformat() if job.ended_at else None,
                                    "description": job.description or "",
                                    "result": job.result,
                                    "meta": job.meta if job.meta else {},
                                    "error_message": str(job.exc_info) if job.exc_info else None
                                })
                            except Exception as e:
                                logger.debug(f"Error fetching job {job_id}: {e}")
                                continue

                return {"client_id": client_id, "jobs": all_jobs}
            except Exception as e:
                logger.error(f"Error fetching jobs for client {client_id}: {e}")
                return {"client_id": client_id, "jobs": []}

        async def fetch_events():
            """Fetch recent system events from the event log (admin only)."""
            if not current_user.is_superuser:
                return []
            try:
                from advanced_omi_backend.services.plugin_service import get_plugin_router

                router_instance = get_plugin_router()
                if not router_instance:
                    return []
                return router_instance.get_recent_events(limit=50)
            except Exception as e:
                logger.error(f"Error fetching events: {e}")
                return []

        # Execute all fetches in parallel (using RQ standard status names)
        queued_jobs_task = fetch_jobs_by_status("queued", limit=100)
        started_jobs_task = fetch_jobs_by_status("started", limit=100)  # RQ standard, not "processing"
        finished_jobs_task = fetch_jobs_by_status("finished", limit=50)  # RQ standard, not "completed"
        failed_jobs_task = fetch_jobs_by_status("failed", limit=50)
        stats_task = fetch_stats()
        streaming_status_task = fetch_streaming_status()
        events_task = fetch_events()
        client_jobs_tasks = [fetch_client_jobs(cid) for cid in expanded_client_ids]

        results = await asyncio.gather(
            queued_jobs_task,
            started_jobs_task,
            finished_jobs_task,
            failed_jobs_task,
            stats_task,
            streaming_status_task,
            events_task,
            *client_jobs_tasks,
            return_exceptions=True
        )

        queued_jobs = results[0] if not isinstance(results[0], Exception) else []
        started_jobs = results[1] if not isinstance(results[1], Exception) else []  # RQ standard
        finished_jobs = results[2] if not isinstance(results[2], Exception) else []  # RQ standard
        failed_jobs = results[3] if not isinstance(results[3], Exception) else []
        stats = results[4] if not isinstance(results[4], Exception) else {"total_jobs": 0}
        streaming_status = results[5] if not isinstance(results[5], Exception) else {"active_sessions": []}
        events = results[6] if not isinstance(results[6], Exception) else []
        recent_conversations = []
        client_jobs_results = results[7:] if len(results) > 7 else []

        # Convert client jobs list to dict
        client_jobs = {}
        for result in client_jobs_results:
            if not isinstance(result, Exception) and result:
                client_jobs[result["client_id"]] = result["jobs"]

        # Convert conversations to dict format for frontend
        conversations_list = []
        for conv in recent_conversations:
            conversations_list.append({
                "conversation_id": conv.conversation_id,
                "user_id": str(conv.user_id) if conv.user_id else None,
                "created_at": conv.created_at.isoformat() if conv.created_at else None,
                "title": conv.title,
                "summary": conv.summary,
                "transcript_text": conv.get_active_transcript_text() if hasattr(conv, 'get_active_transcript_text') else None,
            })

        return {
            "jobs": {
                "queued": queued_jobs,
                "started": started_jobs,  # RQ standard status name
                "finished": finished_jobs,  # RQ standard status name
                "failed": failed_jobs
            },
            "stats": stats,
            "streaming_status": streaming_status,
            "recent_conversations": conversations_list,
            "client_jobs": client_jobs,
            "events": events,
            "timestamp": asyncio.get_event_loop().time()
        }

    except Exception as e:
        logger.error(f"Failed to get dashboard data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get dashboard data: {str(e)}")
