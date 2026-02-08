"""
Job utility functions for RQ workers.

This module provides common utilities for long-running RQ jobs.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def check_job_alive(redis_client, current_job, session_id: Optional[str] = None) -> bool:
    """
    Check if current RQ job still exists in Redis.

    Long-running jobs should call this periodically to detect zombie state
    (when the job has been deleted from Redis but the worker is still running).

    Args:
        redis_client: Async Redis client
        current_job: RQ job instance from get_current_job()
        session_id: Optional session ID to check if session has ended naturally

    Returns:
        False if job is zombie (caller should exit), True otherwise

    Example:
        from rq import get_current_job
        from advanced_omi_backend.utils.job_utils import check_job_alive

        current_job = get_current_job()

        while True:
            # Check for zombie state each iteration
            if not await check_job_alive(redis_client, current_job, session_id):
                break
            # ... do work ...
    """
    if current_job:
        job_exists = await redis_client.exists(f"rq:job:{current_job.id}")
        if not job_exists:
            # Check if this is a natural exit (session ended) vs true zombie
            if session_id:
                session_key = f"audio:session:{session_id}"
                session_status = await redis_client.hget(session_key, "status")
                if session_status and session_status.decode() in ["finalizing", "finished"]:
                    # Session ended naturally - not a zombie, just natural cleanup
                    logger.debug(f"📋 Job {current_job.id} ending naturally (session closed)")
                    return False

            # True zombie - job deleted while session still active
            logger.error(f"🧟 Zombie job detected - job {current_job.id} deleted from Redis while session still active, exiting")
            return False
    return True
