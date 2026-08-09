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
from advanced_omi_backend.models.timeline import TimelineDay
from advanced_omi_backend.services.timeline.discovery import (
    process_timeline_run,
    request_timeline_analysis,
)
from advanced_omi_backend.services.timeline.memory import write_day_memory

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
    outcome = await process_timeline_run(run.run_id)
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
