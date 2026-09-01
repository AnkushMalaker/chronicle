"""Replay one local day's timeline analysis for human review.

The cron path (``process_current_timeline_days`` / ``process_episode_memory``) only ever
looks at today and yesterday, and claims days oldest-first from a shared queue. Neither
is usable for reconstruction, which has to walk a named range of past days in order and
finish each one before the next. This is that entry point.

Rebuild analysis remains chronological, but its memory is no longer written here. The
review queue proposes each day only after the preceding day is accepted or rejected.
"""

import logging
from datetime import date

from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.models.timeline import (
    DirtyEvidenceRange,
    ImmichEvidenceSummary,
    ImmichVisualPreparationStatus,
    TimelineDay,
    TimelineReconciliationRequest,
    utcnow,
)
from advanced_omi_backend.services.timeline.consolidation import (
    generate_day_consolidation,
)
from advanced_omi_backend.services.timeline.dirty_ranges import release_range_for_retry
from advanced_omi_backend.services.timeline.discovery import (
    process_timeline_run,
    request_timeline_analysis,
)
from advanced_omi_backend.services.timeline.evidence import (
    assemble_day_evidence,
    summarize_immich_evidence,
)
from advanced_omi_backend.services.timeline.executor import settings_dict
from advanced_omi_backend.services.timeline.immich_visual_evidence import (
    prepare_immich_visual_evidence,
)
from advanced_omi_backend.services.timeline.memory import write_day_memory
from advanced_omi_backend.services.timeline.reconciliation import (
    finish_range,
    lease_range_by_id,
    reconcile_range,
)

logger = logging.getLogger(__name__)


def _last_immich_evidence(accounting: list[dict]) -> ImmichEvidenceSummary:
    for iteration in reversed(accounting):
        if iteration.get("immich_evidence") is not None:
            return ImmichEvidenceSummary.model_validate(iteration["immich_evidence"])
    return ImmichEvidenceSummary()


@async_job(redis=True, beanie=True)
async def explicit_reconciliation_job(request_id: str, *, redis_client=None) -> dict:
    """Real registered entrypoint for the sole explicit Timeline write path."""

    request = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request_id
    )
    if request is None:
        return {"request_id": request_id, "state": "missing"}
    request.state = "running"
    request.last_error = None
    request.updated_at = utcnow()
    await request.save()
    leased = None
    try:
        if request.target_asset_count > 0:
            request.immich_visual = ImmichVisualPreparationStatus(state="running")
            request.updated_at = utcnow()
            await request.save()
            visual = await prepare_immich_visual_evidence(
                request.user_id, request.local_date, request.timezone
            )
            request.immich_visual = ImmichVisualPreparationStatus.model_validate(
                visual.payload()
            )
            request.updated_at = utcnow()
            await request.save()
            if visual.state == "failed":
                raise RuntimeError(
                    "all selected Immich photos failed visual preparation"
                )
        elif request.immich_visual is None:
            request.immich_visual = ImmichVisualPreparationStatus(state="not_needed")

        if request.pipeline == "rolling":
            if not request.dirty_range_id:
                raise RuntimeError("explicit rolling request has no dirty range")
            dirty_range = await DirtyEvidenceRange.find_one(
                DirtyEvidenceRange.dirty_range_id == request.dirty_range_id
            )
            if dirty_range is None:
                raise RuntimeError("authorized dirty range is missing")
            owner = f"explicit_reconciliation:{request_id}"
            leased = await lease_range_by_id(dirty_range.dirty_range_id, owner)
            if leased is None:
                raise RuntimeError("authorized dirty range could not be leased")
            accounting: list[dict] = []
            outcome = await reconcile_range(leased, accounting=accounting)
            request.immich_evidence = _last_immich_evidence(accounting)
            request.updated_at = utcnow()
            await request.save()
            range_state = await finish_range(leased, outcome)
            if range_state != "completed":
                raise RuntimeError(f"reconciliation ended in {range_state}")
            result = {
                "request_id": request_id,
                "dirty_range_id": leased.dirty_range_id,
                "published": list(outcome.episode_ids) if outcome else [],
                "iterations": accounting,
            }
        else:
            run = await request_timeline_analysis(
                request.user_id,
                request.local_date,
                request.timezone,
                force=True,
            )
            request.run_id = run.run_id
            request.updated_at = utcnow()
            await request.save()
            outcome = await process_timeline_run(run.run_id)
            if request.target_asset_count:
                timeline_settings = settings_dict()
                manifest, _images = await assemble_day_evidence(
                    request.user_id,
                    request.local_date,
                    request.timezone,
                    window_minutes=int(timeline_settings.get("window_minutes", 20)),
                    overlap_minutes=int(timeline_settings.get("overlap_minutes", 3)),
                )
                request.immich_evidence = summarize_immich_evidence(manifest)
            else:
                request.immich_evidence = ImmichEvidenceSummary()
            request.updated_at = utcnow()
            await request.save()
            result = {"request_id": request_id, "run_id": run.run_id, **outcome}
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
        if request.pipeline == "rolling" and leased is not None:
            released = await release_range_for_retry(leased, error_text)
            request.state = "failed" if released.state == "failed" else "queued"
        else:
            request.state = "failed"
        request.last_error = error_text
        request.updated_at = utcnow()
        await request.save()
        raise
    request.state = "completed"
    request.updated_at = utcnow()
    await request.save()
    return {**result, "state": "completed"}


@async_job(redis=True, beanie=True)
async def generate_timeline_consolidation_job(
    user_id: str, local_date: str, timezone_name: str, run_id: str, *, redis_client=None
) -> dict:
    """Pre-generate a day grouping proposal outside timeline publication."""

    return await generate_day_consolidation(
        user_id, date.fromisoformat(local_date), timezone_name, run_id
    )


@async_job(redis=True, beanie=True)
async def rebuild_timeline_day_job(
    user_id: str,
    local_date: str,
    timezone_name: str,
    *,
    write_memory: bool = False,
    redis_client=None,
) -> dict:
    """Re-analyze one local day from evidence, leaving it for episode review.

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
        result["memory"] = "pending_review"
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
async def record_timeline_day_memory_job(
    user_id: str,
    local_date: str,
    timezone_name: str,
    *,
    redis_client=None,
) -> dict:
    """Retry only the vault write for an already-published Timeline day.

    A ``partial`` memory state means Timeline already published an active generation
    and Pi did not finish recording it. Re-running segmentation here is both wasteful
    and unsafe: a fresh candidate can fail coverage validation and prevent the valid
    active generation from ever reaching the vault.
    """

    day = date.fromisoformat(local_date)
    stored = await TimelineDay.find_one(
        TimelineDay.user_id == user_id,
        TimelineDay.local_date == day,
        TimelineDay.timezone == timezone_name,
    )
    if stored is None or not stored.active_run_id:
        raise RuntimeError(
            f"Cannot repair memory for {local_date}: no active Timeline generation"
        )
    result = await write_day_memory(stored, retry_partial=True)
    logger.info(
        "🗓️ Repair recorded %s for user %s from active Timeline generation: %s",
        local_date,
        user_id,
        result,
    )
    return {"local_date": local_date, "memory": result}


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
