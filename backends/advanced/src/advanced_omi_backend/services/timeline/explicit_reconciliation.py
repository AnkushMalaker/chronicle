"""Explicit, Immich-gated authorization for Timeline reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from advanced_omi_backend.models.timeline import (
    ImmichVisualPreparationStatus,
    TimelineReconciliationRequest,
    utcnow,
)
from advanced_omi_backend.services.immich_discovery import (
    check_immich_day_readiness,
    import_immich_day_candidates,
    resolve_immich_user_id,
)
from advanced_omi_backend.services.notifications import (
    NotificationCommand,
    enqueue_notification,
)
from advanced_omi_backend.services.redis_lock import distributed_lock
from advanced_omi_backend.services.timeline.dirty_ranges import mark_evidence_dirty
from advanced_omi_backend.users import User

# The readiness check can page through several Immich search windows before importing
# bounded candidates. Keep the cross-process claim longer than that full HTTP budget.
EXPLICIT_REQUEST_LOCK_SECONDS = 15 * 60
CONCURRENT_REQUEST_REUSE_SECONDS = 10


def day_bounds(local_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    started_at = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    ended_at = datetime.combine(
        local_date + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return started_at, ended_at


async def request_explicit_reconciliation(
    *, user: User, local_date: date, timezone_name: str
) -> tuple[TimelineReconciliationRequest, bool]:
    """Check Immich now and either block or authorize exactly one day."""

    user_id = str(user.id)
    immich_owner_id = await resolve_immich_user_id()
    if immich_owner_id != user_id:
        raise PermissionError(
            "Immich reconciliation is available only to its configured Chronicle owner"
        )
    claim = hashlib.sha256(
        f"{user_id}\0{local_date.isoformat()}\0{timezone_name}".encode()
    ).hexdigest()
    async with distributed_lock(
        f"timeline:explicit-request:{claim}",
        timeout=EXPLICIT_REQUEST_LOCK_SECONDS,
        blocking_timeout=EXPLICIT_REQUEST_LOCK_SECONDS,
    ):
        return await _request_explicit_reconciliation_locked(
            user=user,
            local_date=local_date,
            timezone_name=timezone_name,
        )


async def _request_explicit_reconciliation_locked(
    *, user: User, local_date: date, timezone_name: str
) -> tuple[TimelineReconciliationRequest, bool]:
    """Create or reuse a request while its cross-process day claim is held."""

    user_id = str(user.id)
    # Reuse durable work, plus a just-created blocked result from another request that
    # was waiting on this same Redis claim. A later explicit "Check again" remains a
    # fresh live Immich check rather than being pinned to an old blocked snapshot.
    active = (
        await TimelineReconciliationRequest.find(
            TimelineReconciliationRequest.user_id == user_id,
            TimelineReconciliationRequest.local_date == local_date,
            TimelineReconciliationRequest.timezone == timezone_name,
            {
                "$or": [
                    {"state": {"$in": ["queued", "running"]}},
                    {
                        "state": "blocked",
                        "created_at": {
                            "$gte": utcnow()
                            - timedelta(seconds=CONCURRENT_REQUEST_REUSE_SECONDS)
                        },
                    },
                ]
            },
        )
        .sort("-created_at")
        .first_or_none()
    )
    if active is not None:
        return active, False

    readiness = await check_immich_day_readiness(local_date, timezone_name)
    pipeline = "rolling" if user.active_timeline_pipeline == "rolling" else "day"
    request = TimelineReconciliationRequest(
        user_id=user_id,
        local_date=local_date,
        timezone=timezone_name,
        pipeline=pipeline,
        state="queued" if readiness.ready else "blocked",
        reason=readiness.reason,
        target_asset_count=readiness.target_asset_count,
        latest_asset_local_date=readiness.latest_asset_local_date,
        checked_at=readiness.checked_at,
        immich_visual=ImmichVisualPreparationStatus(
            state="pending" if readiness.target_asset_count else "not_needed"
        ),
    )
    await request.insert()

    if not readiness.ready:
        if readiness.reason == "no_immich_evidence":
            try:
                intent, _created = await enqueue_notification(
                    user_id=user_id,
                    command=NotificationCommand(
                        notification_type="priority",
                        title="Timeline is waiting for Immich",
                        body=(
                            f"Open Immich and let backup run for {local_date.isoformat()}, "
                            "then choose Check again in Chronicle."
                        ),
                        action="open_immich",
                        expires_at=utcnow() + timedelta(hours=2),
                        dedupe_key=f"immich-backup:{local_date.isoformat()}",
                    ),
                    source="timeline_immich_gate",
                    actor_id=user_id,
                )
                request.notification_id = intent.notification_id
                request.notification_status = intent.state
            except Exception as error:
                request.notification_status = "failed"
                request.last_error = f"Backup reminder could not be queued: {error}"
            request.updated_at = utcnow()
            await request.save()
        return request, True

    await import_immich_day_candidates(user_id, readiness.target_assets)
    if pipeline == "rolling":
        started_at, ended_at = day_bounds(local_date, timezone_name)
        dirty_range = await mark_evidence_dirty(
            user_id,
            started_at,
            ended_at,
            request.request_id,
            "explicit_immich_ready_day",
            source_kind="manual",
            not_before=utcnow(),
            coalesce=False,
        )
        dirty_range.dispatch_authorized_at = utcnow()
        dirty_range.reconciliation_request_id = request.request_id
        await dirty_range.save()
        request.dirty_range_id = dirty_range.dirty_range_id

    job_id = await asyncio.to_thread(_queue_request, request.request_id)
    if not job_id:
        request.state = "failed"
        request.last_error = "failed to enqueue explicit reconciliation"
    else:
        request.job_id = job_id
    request.updated_at = utcnow()
    await request.save()
    return request, True


def _queue_request(request_id: str) -> str | None:
    # Lazy to avoid coupling the Timeline service import graph to worker bootstrap.
    from advanced_omi_backend.controllers.queue_controller import (
        enqueue_explicit_timeline_reconciliation,
    )

    return enqueue_explicit_timeline_reconciliation(request_id)


def reconciliation_request_payload(request: TimelineReconciliationRequest) -> dict:
    return {
        "request_id": request.request_id,
        "date": request.local_date,
        "timezone": request.timezone,
        "pipeline": request.pipeline,
        "state": request.state,
        "reason": request.reason,
        "target_asset_count": request.target_asset_count,
        "latest_eligible_asset_date": request.latest_asset_local_date,
        "checked_at": request.checked_at,
        "notification_id": request.notification_id,
        "notification_status": request.notification_status,
        "job_id": request.job_id,
        "dirty_range_id": request.dirty_range_id,
        "run_id": request.run_id,
        "immich_visual": (
            request.immich_visual.model_dump(mode="json")
            if request.immich_visual
            else None
        ),
        "immich_evidence": (
            request.immich_evidence.model_dump(mode="json")
            if request.immich_evidence
            else None
        ),
        "last_error": request.last_error,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
    }
