"""Replay one local day's timeline analysis and its vault record.

The cron path (``process_current_timeline_days`` / ``process_episode_memory``) only ever
looks at today and yesterday, and claims days oldest-first from a shared queue. Neither
is usable for reconstruction, which has to walk a named range of past days in order and
finish each one before the next. This is that entry point.

Analysis and the day write are one job on purpose: they must not interleave across days,
because the write agent holds the per-user vault lock and a second day waiting on it
inside its own job would burn a worker slot doing nothing.
"""

import logging
from datetime import date

from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.models.timeline import DirtyEvidenceRange, TimelineDay
from advanced_omi_backend.services.timeline.dirty_ranges import complete_range
from advanced_omi_backend.services.timeline.discovery import (
    process_timeline_run,
    request_timeline_analysis,
)
from advanced_omi_backend.services.timeline.memory import write_day_memory
from advanced_omi_backend.services.timeline.reconciliation import (
    finish_range,
    lease_range_by_id,
    reconcile_range,
)

logger = logging.getLogger(__name__)


@async_job(redis=True, beanie=True)
async def rebuild_timeline_day_job(
    user_id: str,
    local_date: str,
    timezone_name: str,
    *,
    write_memory: bool = True,
    redis_client=None,
) -> dict:
    """Re-analyze one local day from evidence, then record it in the vault.

    ``force=True`` is required: a run is keyed by its evidence revision, so an
    unchanged day would return the existing completed run and re-publish nothing.
    """

    day = date.fromisoformat(local_date)
    run = await request_timeline_analysis(user_id, day, timezone_name, force=True)
    # Rebuilds are clean-slate for model-authored episodes. Otherwise the old
    # generation is offered as prior context and can anchor the model so strongly that
    # newly recovered recordings are ignored. Confirmed/pinned episodes are retained
    # independently by the discovery service.
    outcome = await process_timeline_run(run.run_id, retain_unconfirmed_existing=False)
    logger.info("🗓️ Rebuild analysed %s for user %s: %s", local_date, user_id, outcome)
    result = {"local_date": local_date, "run_id": run.run_id, **outcome}
    if not write_memory:
        return result

    stored = await TimelineDay.find_one(
        TimelineDay.user_id == user_id,
        TimelineDay.local_date == day,
        TimelineDay.timezone == timezone_name,
    )
    if stored is None or not stored.active_run_id:
        # Analysis produced no published day — an empty day, or a failed run that has
        # already recorded its own error. Nothing to write, and nothing to retry here.
        result["memory"] = "no_day"
        return result
    result["memory"] = await write_day_memory(stored)
    logger.info(
        "🗓️ Rebuild recorded %s for user %s: %s", local_date, user_id, result["memory"]
    )
    return result


@async_job(redis=True, beanie=True)
async def reconcile_range_job(dirty_range_id: str, redis_client=None) -> dict:
    """Reconcile one dirty evidence range: lease it, run it, terminate it.

    The lease is claimed *by id* rather than through ``lease_due_range``, which takes
    the globally oldest due range and would have this job silently reconcile someone
    else's interval. A run that parks or fences the range leaves it schedulable; only a
    completed publish or no-op closes it.
    """

    dirty_range = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range_id
    )
    if dirty_range is None:
        return {"dirty_range_id": dirty_range_id, "state": "missing"}
    if dirty_range.state in ("completed", "failed"):
        # Another worker already finished it; the enqueue lock only collapses bursts.
        return {"dirty_range_id": dirty_range_id, "state": dirty_range.state}

    owner = f"reconcile_range_job:{dirty_range_id}"
    leased = await lease_range_by_id(dirty_range_id, owner)
    if leased is None:
        return {"dirty_range_id": dirty_range_id, "state": "not_leased"}

    logger.info(
        "🩹 Reconciling dirty range %s (%s → %s, revision %s)",
        leased.dirty_range_id,
        leased.started_at.isoformat(),
        leased.ended_at.isoformat(),
        leased.leased_evidence_revision,
    )
    accounting: list[dict] = []
    try:
        outcome = await reconcile_range(leased, accounting=accounting)
    except Exception as error:
        logger.exception("🩹 Reconciliation of %s failed", dirty_range_id)
        await complete_range(leased, error=f"{type(error).__name__}: {error}")
        return {
            "dirty_range_id": dirty_range_id,
            "state": "failed",
            "error": str(error),
            "iterations": accounting,
        }

    state = await finish_range(leased, outcome)
    return {
        "dirty_range_id": dirty_range_id,
        "state": state,
        "evidence_revision": leased.leased_evidence_revision,
        "iterations": accounting,
        "published": list(outcome.episode_ids) if outcome else [],
        "fenced": bool(outcome.fenced) if outcome else None,
        "material_change": bool(outcome.material_change) if outcome else False,
        "affected_local_dates": (
            [value.isoformat() for value in outcome.affected_local_dates]
            if outcome
            else []
        ),
    }
