"""Bounded, shared progress snapshots and activity events on the owning RQ job."""

import asyncio
import logging
from datetime import datetime, timezone

from rq import get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Job

from backend.redis_factory import create_sync_redis
from backend.services.sse_publisher import publish_sse_event

logger = logging.getLogger(__name__)

STAGES = [
    ("photos", "Inspect photos"),
    ("evidence", "Prepare evidence"),
    ("context", "Summarize context"),
    ("separation", "Form episodes"),
    ("interpretation", "Interpret & validate"),
    ("publication", "Publish timeline"),
]


def _update(
    job, stage, message, *, completed, total, unit, state, attempt, user_id, reset
):
    stamp = datetime.now(timezone.utc).isoformat()
    progress = job.meta.get("progress") or {
        "started_at": stamp,
        "stages": [
            {"id": key, "label": label, "state": "waiting"} for key, label in STAGES
        ],
        "events": [],
    }
    if user_id:
        progress["user_id"] = user_id
    if reset:
        progress["stages"] = [
            {"id": key, "label": label, "state": "waiting"} for key, label in STAGES
        ]
    stage = stage or progress.get("stage") or "photos"
    item = next(row for row in progress["stages"] if row["id"] == stage)
    item.update(state=state, attempt=attempt)
    if completed is not None:
        item["completed"] = completed
    if total is not None:
        item["total"] = total
    if unit is not None:
        item["unit"] = unit
    progress.update(stage=stage, message=message, updated_at=stamp)
    progress["events"] = (
        progress["events"]
        + [
            {
                "at": stamp,
                "stage": stage,
                "message": message,
                "state": state,
                "attempt": attempt,
            }
        ]
    )[-80:]
    job.meta["progress"] = progress
    job.save_meta()
    if progress.get("user_id"):
        publish_sse_event(
            progress["user_id"],
            "job.progress",
            {
                "job_id": job.id,
                "reconciliation_request_id": job.meta.get("reconciliation_request_id"),
                "progress": progress,
            },
        )


async def report_job_progress(
    stage,
    message,
    *,
    completed=None,
    total=None,
    unit=None,
    state="running",
    attempt=1,
    user_id=None,
    reset=False
):
    """Observability cannot fail the underlying work; Redis I/O stays off the loop."""
    job = get_current_job()
    if job is None or "reconciliation_request_id" not in job.meta:
        return
    try:
        await asyncio.to_thread(
            _update,
            job,
            stage,
            message,
            completed=completed,
            total=total,
            unit=unit,
            state=state,
            attempt=attempt,
            user_id=user_id,
            reset=reset,
        )
    except Exception:
        logger.warning("Unable to persist progress for job %s", job.id, exc_info=True)


def read_job_progress(job_id):
    if not job_id:
        return None
    connection = create_sync_redis()
    try:
        job = Job.fetch(job_id, connection=connection)
        progress = job.meta.get("progress")
        if progress is None:
            return None
        return {
            **progress,
            "job_id": job.id,
            "job_status": str(job.get_status().value),
            "heartbeat_at": (
                job.last_heartbeat.isoformat() if job.last_heartbeat else None
            ),
            "ended_at": job.ended_at.isoformat() if job.ended_at else None,
        }
    except NoSuchJobError:
        return None
    except Exception:
        logger.warning("Unable to read job progress for %s", job_id, exc_info=True)
        return None
    finally:
        connection.close()
