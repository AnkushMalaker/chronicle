"""Reap orphaned deferred RQ jobs.

A deferred job is *orphaned* when nothing will ever promote it: none of its
dependencies is still pending — each is either missing from Redis (evicted /
hard-deleted) or already in a terminal state.

RQ only ever promotes a deferred job from its dependency's success/failure
handler (``enqueue_dependents``). If that dependency was *deleted* there is no
completion or failure event to fire, so the dependent — and the whole downstream
chain hanging off it — sits in the ``DeferredJobRegistry`` forever, with no TTL
to age it out. It just accumulates on the queue page.

The post-conversation chain's ``Retry`` + ``Dependency(allow_failure=True)`` +
``on_failure`` recovery covers a dependency that *fails*, not one that *vanishes*
(a deleted job emits no event at all), so those orphans need a sweep. This module
is that sweep's logic, shared by:

- the periodic backstop reaper (:mod:`backend.services.reaper`), and
- the manual ``scripts/purge_orphaned_deferred_jobs.py`` CLI.

All calls here are synchronous RQ/Redis operations; the async reaper runs them in
a thread.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import DeferredJobRegistry

from backend.controllers.queue_controller import (
    QUEUE_NAMES,
    get_job_status_from_rq,
    get_queue,
    redis_conn,
)

logger = logging.getLogger(__name__)

# Dependency states that mean a deferred job could still legitimately be promoted.
PENDING_STATES = {"queued", "started", "deferred", "scheduled"}

# Don't reap a freshly-deferred job: give RQ's own promotion a wide margin so the
# sweep can never race a dependent that's about to be enqueued (the dependency
# just finished but ``enqueue_dependents`` hasn't run yet). Real orphans are
# hours-to-days old; this only needs to clear that sub-second in-flight window.
DEFAULT_MIN_AGE_SECS = int(os.getenv("DEFERRED_ORPHAN_MIN_AGE_SECS", "1800"))


def _dependency_ids(job: Job) -> list:
    # RQ loads the job hash's dependency ids into ``_dependency_ids`` on fetch.
    return list(getattr(job, "_dependency_ids", None) or [])


def _job_age_secs(job: Job) -> Optional[float]:
    created = getattr(job, "created_at", None)
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def _is_orphaned(job: Job) -> tuple[bool, str]:
    """Return ``(orphaned, reason)``. Orphaned == no dependency is still pending."""
    deps = _dependency_ids(job)
    if not deps:
        # A deferred job with no recorded dependency can never be promoted.
        return True, "no dependencies recorded"
    missing, terminal, pending = [], [], []
    for dep_id in deps:
        try:
            dep = Job.fetch(dep_id, connection=redis_conn)
        except NoSuchJobError:
            missing.append(dep_id)
            continue
        status = get_job_status_from_rq(dep)
        if status in PENDING_STATES:
            pending.append(f"{dep_id}={status}")
        else:
            terminal.append(f"{dep_id}={status}")
    if pending:
        return False, f"live dependency: {pending}"
    return True, f"missing={missing} terminal={terminal}"


def find_orphaned_deferred_jobs(*, min_age_secs: float = DEFAULT_MIN_AGE_SECS) -> list:
    """Scan every queue's DeferredJobRegistry for orphaned jobs.

    Returns a list of ``(queue_name, job_id, conversation_id, reason)``. Jobs
    younger than ``min_age_secs`` are skipped so an in-flight chain is never
    mistaken for an orphan.
    """
    orphans = []
    for queue_name in QUEUE_NAMES:
        queue = get_queue(queue_name)
        for job_id in DeferredJobRegistry(queue=queue).get_job_ids():
            try:
                job = Job.fetch(job_id, connection=redis_conn)
            except NoSuchJobError:
                continue
            age = _job_age_secs(job)
            if age is not None and age < min_age_secs:
                continue
            orphaned, reason = _is_orphaned(job)
            if orphaned:
                conv = (job.meta or {}).get("conversation_id", "?")
                orphans.append((queue_name, job_id, conv, reason))
    return orphans


def reap_orphaned_deferred_jobs(
    *, min_age_secs: float = DEFAULT_MIN_AGE_SECS, max_passes: int = 20
) -> dict:
    """Delete orphaned deferred jobs and return ``{'deleted': n, 'details': [...]}``.

    Multi-pass because deleting a head orphan turns its dependents into orphans
    (their dependency is now missing), so we iterate until a pass deletes nothing
    new or no orphan remains.
    """
    deleted: list[dict] = []
    for _ in range(max_passes):
        orphans = find_orphaned_deferred_jobs(min_age_secs=min_age_secs)
        if not orphans:
            break
        progressed = False
        for queue_name, job_id, conv, reason in orphans:
            try:
                Job.fetch(job_id, connection=redis_conn).delete(remove_from_queue=True)
                deleted.append(
                    {
                        "queue": queue_name,
                        "job_id": job_id,
                        "conversation_id": conv,
                        "reason": reason,
                    }
                )
                progressed = True
            except NoSuchJobError:
                continue
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to delete orphaned deferred job {job_id}: {e}")
        if not progressed:
            break
    return {"deleted": len(deleted), "details": deleted}
