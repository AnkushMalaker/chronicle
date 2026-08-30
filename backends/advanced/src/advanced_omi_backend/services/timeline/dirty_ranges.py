"""Dirty-range scheduling for rolling reconciliation.

Every evidence producer — recording closure, transcript revision, speaker revision,
silence trim, evidence spans, manual memories — calls :func:`mark_evidence_dirty` with
the absolute interval its change touched. Nearby ``pending``/``waiting`` rows coalesce
into one, so a burst of revisions over the same minutes costs one reconciliation.

Two clocks live on the row. ``not_before`` is the debounce: each new trigger pushes it
out, so reconciliation waits for evidence to stop moving. ``force_after`` is the
liveness bound, and merging keeps the *earliest* one, so continuous media can never
postpone a first look indefinitely.

A ``leased`` row is never coalesced into. The lease snapshots ``evidence_revision``
into ``leased_evidence_revision``; the run reconciles that snapshot and fences its
publish on it, while a trigger arriving mid-run opens a fresh ``pending`` row over the
same interval. That is what keeps continuous evidence from livelocking the fence:
forced progress guarantees a run starts, the snapshot guarantees it can finish, and the
fresh row guarantees nothing is lost.

See ``docs/backend/rolling-reconciliation.md`` → "Dirty-range scheduling".
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from pymongo import ReturnDocument

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import DirtyEvidenceRange, utcnow
from advanced_omi_backend.redis_factory import create_async_redis
from advanced_omi_backend.redis_keys import timeline_evidence_revision

logger = logging.getLogger(__name__)

# Overridable at module level so tests can compress the schedule without patching
# every call site. See the module docstring for what each clock means.
DEBOUNCE_MINUTES = 5
FORCE_AFTER_MINUTES = 15
COALESCE_GAP_MINUTES = 5
LEASE_MINUTES = 30
MAX_ATTEMPTS = 5
# One scan enqueues at most this many ranges, so a large backlog cannot turn a cron
# tick on the API event loop into a long Mongo/Redis burst.
SCAN_BATCH = 50

_COALESCABLE_STATES = ("pending", "waiting")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _next_evidence_revision(user_id: str) -> int:
    """Bump the per-user monotonic evidence counter."""
    client = create_async_redis(decode_responses=True)
    try:
        return int(await client.incr(timeline_evidence_revision(user_id)))
    finally:
        await client.aclose()


def _merge_ordered(existing: Sequence[str], additions: Sequence[str]) -> list[str]:
    """Union two sequences preserving first-seen order."""
    merged = list(existing)
    seen = set(merged)
    for item in additions:
        if item and item not in seen:
            merged.append(item)
            seen.add(item)
    return merged


async def mark_evidence_dirty(
    user_id: str,
    started_at: datetime,
    ended_at: datetime,
    source_revision: str,
    reason: str,
    *,
    source_kind: str = "generic",
    not_before: Optional[datetime] = None,
) -> DirtyEvidenceRange:
    """Record that ``[started_at, ended_at)`` needs reconciliation. Idempotent.

    Overlapping or nearby (within ``COALESCE_GAP_MINUTES``) ``pending``/``waiting``
    rows are folded into one row spanning min-start/max-end, carrying the union of
    source revisions and trigger reasons. The debounce restarts; the earliest existing
    ``force_after`` is preserved. A ``waiting`` row overlapped by a trigger wakes back
    to ``pending``.

    ``not_before`` overrides the debounce — that is how ``force`` on a manual
    reconcile request asks for the range to be looked at now.
    """

    started_at = _as_utc(started_at)
    ended_at = _as_utc(ended_at)
    if ended_at <= started_at:
        raise ValueError("dirty evidence range must have positive duration")

    now = utcnow()
    revision = await _next_evidence_revision(user_id)
    gap = timedelta(minutes=COALESCE_GAP_MINUTES)

    neighbours = (
        await DirtyEvidenceRange.find(
            {
                "user_id": user_id,
                "state": {"$in": list(_COALESCABLE_STATES)},
                "started_at": {"$lte": ended_at + gap},
                "ended_at": {"$gte": started_at - gap},
            }
        )
        .sort("+created_at")
        .to_list()
    )

    debounce = (
        _as_utc(not_before) if not_before else now + timedelta(minutes=DEBOUNCE_MINUTES)
    )

    if not neighbours:
        row = DirtyEvidenceRange(
            user_id=user_id,
            started_at=started_at,
            ended_at=ended_at,
            evidence_revision=revision,
            source_revisions=(
                {source_kind: [source_revision]} if source_revision else {}
            ),
            trigger_reasons=[reason],
            not_before=debounce,
            force_after=now + timedelta(minutes=FORCE_AFTER_MINUTES),
        )
        await row.insert()
        logger.debug(
            "🩹 Dirty range opened %s for %s (%s)", row.dirty_range_id, user_id, reason
        )
        return row

    survivor, *duplicates = neighbours
    survivor.started_at = min(
        [started_at] + [_as_utc(row.started_at) for row in neighbours]
    )
    survivor.ended_at = max([ended_at] + [_as_utc(row.ended_at) for row in neighbours])

    source_revisions: dict[str, list[str]] = {}
    reasons: list[str] = []
    for row in neighbours:
        for kind, values in (row.source_revisions or {}).items():
            source_revisions[kind] = _merge_ordered(
                source_revisions.get(kind, []), values
            )
        reasons = _merge_ordered(reasons, row.trigger_reasons or [])
    if source_revision:
        source_revisions[source_kind] = _merge_ordered(
            source_revisions.get(source_kind, []), [source_revision]
        )
    survivor.source_revisions = source_revisions
    survivor.trigger_reasons = _merge_ordered(reasons, [reason])

    survivor.evidence_revision = revision
    survivor.not_before = debounce
    # Earliest existing deadline wins: coalescing must never extend how long a range
    # can go unlooked-at.
    survivor.force_after = min(_as_utc(row.force_after) for row in neighbours)
    survivor.state = "pending"
    survivor.lease_owner = None
    survivor.lease_expires_at = None
    survivor.updated_at = now
    await survivor.save()

    for row in duplicates:
        await row.delete()

    logger.debug(
        "🩹 Dirty range %s coalesced %d row(s) for %s (%s)",
        survivor.dirty_range_id,
        len(duplicates),
        user_id,
        reason,
    )
    return survivor


def _due_or_reclaimable(now: datetime) -> dict[str, Any]:
    return {
        "$or": [
            {
                "state": "pending",
                "attempts": {"$lt": MAX_ATTEMPTS},
                "$or": [{"not_before": {"$lte": now}}, {"force_after": {"$lte": now}}],
            },
            {
                "state": "leased",
                "attempts": {"$lt": MAX_ATTEMPTS},
                "lease_expires_at": {"$lt": now},
            },
        ]
    }


async def _fail_exhausted(now: datetime) -> int:
    """Terminate ranges that have burned every attempt."""
    collection = DirtyEvidenceRange.get_pymongo_collection()
    result = await collection.update_many(
        {
            "state": {"$in": ["pending", "leased"]},
            "attempts": {"$gte": MAX_ATTEMPTS},
        },
        {
            "$set": {
                "state": "failed",
                "last_error": f"exhausted {MAX_ATTEMPTS} reconciliation attempts",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )
    return int(result.modified_count)


async def lease_due_range(
    owner: str, now: Optional[datetime] = None
) -> Optional[DirtyEvidenceRange]:
    """Compare-and-swap claim of one due range, oldest deadline first.

    Also reclaims a ``leased`` row whose lease expired — a worker that died holding a
    range must not strand it. A range that has already used ``MAX_ATTEMPTS`` leases is
    marked ``failed`` rather than leased again.
    """

    now = _as_utc(now) if now else utcnow()
    await _fail_exhausted(now)

    collection = DirtyEvidenceRange.get_pymongo_collection()
    document = await collection.find_one_and_update(
        _due_or_reclaimable(now),
        [
            {
                "$set": {
                    "state": "leased",
                    "lease_owner": owner,
                    "lease_expires_at": now + timedelta(minutes=LEASE_MINUTES),
                    "attempts": {"$add": ["$attempts", 1]},
                    "leased_evidence_revision": "$evidence_revision",
                    "last_error": None,
                    "updated_at": now,
                }
            }
        ],
        sort=[("force_after", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        return None
    return await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == document["dirty_range_id"]
    )


async def complete_range(
    dirty_range: DirtyEvidenceRange, *, error: Optional[str] = None
) -> DirtyEvidenceRange:
    """Terminate a leased range as ``completed`` or, with ``error``, ``failed``."""

    dirty_range.state = "failed" if error else "completed"
    dirty_range.last_error = error
    dirty_range.lease_owner = None
    dirty_range.lease_expires_at = None
    dirty_range.updated_at = utcnow()
    await dirty_range.save()
    return dirty_range


async def park_waiting(
    dirty_range: DirtyEvidenceRange, reason: str
) -> DirtyEvidenceRange:
    """Park a range that needs future evidence before it can be reconciled.

    It stays schedulable: ``not_before`` becomes a fallback wake so a range whose
    awaited evidence never arrives is still looked at again, and an overlapping
    trigger wakes it immediately via :func:`mark_evidence_dirty`.
    """

    now = utcnow()
    dirty_range.state = "waiting"
    dirty_range.not_before = now + timedelta(minutes=FORCE_AFTER_MINUTES)
    dirty_range.trigger_reasons = _merge_ordered(
        dirty_range.trigger_reasons or [], [f"waiting:{reason}"]
    )
    dirty_range.lease_owner = None
    dirty_range.lease_expires_at = None
    dirty_range.updated_at = now
    await dirty_range.save()
    return dirty_range


async def reap_expired_leases(now: Optional[datetime] = None) -> int:
    """Return expired leases to ``pending``. Returns how many were reclaimed."""

    now = _as_utc(now) if now else utcnow()
    collection = DirtyEvidenceRange.get_pymongo_collection()
    result = await collection.update_many(
        {"state": "leased", "lease_expires_at": {"$lt": now}},
        {
            "$set": {
                "state": "pending",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )
    reclaimed = int(result.modified_count)
    if reclaimed:
        logger.info("🩹 Reclaimed %d expired dirty-range lease(s)", reclaimed)
    return reclaimed


async def due_ranges(
    now: Optional[datetime] = None, limit: int = SCAN_BATCH
) -> list[DirtyEvidenceRange]:
    """Pending ranges whose debounce elapsed or whose forced deadline passed."""

    now = _as_utc(now) if now else utcnow()
    return (
        await DirtyEvidenceRange.find(
            {
                "state": "pending",
                "attempts": {"$lt": MAX_ATTEMPTS},
                "$or": [
                    {"not_before": {"$lte": now}},
                    {"force_after": {"$lte": now}},
                ],
            }
        )
        .sort("+force_after")
        .limit(limit)
        .to_list()
    )


async def reconcile_dirty_ranges() -> dict[str, int]:
    """Cron entry point: enqueue dirty ranges and recover classified dispatches.

    Deliberately cheap — it runs on the API event loop, so it does Mongo queries and
    RQ enqueues plus one bounded unlatched-Episode scan. All agent/model work
    happens in ``reconcile_range_job``.
    """

    # Imported here to avoid a circular import with the controllers package.
    from advanced_omi_backend.controllers.queue_controller import (
        enqueue_dirty_range_reconciliation,
    )
    from advanced_omi_backend.services.timeline.dispatch import dispatch_ready_episodes

    now = utcnow()
    reclaimed = await reap_expired_leases(now)
    await _fail_exhausted(now)
    ranges = await due_ranges(now)

    enqueued = 0
    for dirty_range in ranges:
        job_id = await asyncio.to_thread(
            enqueue_dirty_range_reconciliation, dirty_range.dirty_range_id
        )
        if job_id:
            enqueued += 1

    recovery = await dispatch_ready_episodes()

    if ranges or reclaimed:
        logger.info(
            "🩹 Rolling reconciliation scan: %d due, %d enqueued, %d lease(s) reclaimed",
            len(ranges),
            enqueued,
            reclaimed,
        )
    return {
        "due": len(ranges),
        "enqueued": enqueued,
        "reclaimed": reclaimed,
        "dispatched": recovery["dispatched"],
    }


# ── Producer-side trigger helpers ────────────────────────────────────────────
#
# A producer must never fail because scheduling failed: the recovery scan and the
# next trigger both re-open the range, while a raised exception would break audio
# processing. These wrappers log and swallow.


async def note_evidence_dirty(
    user_id: str,
    started_at: Optional[datetime],
    ended_at: Optional[datetime],
    source_revision: str,
    reason: str,
    *,
    source_kind: str = "generic",
) -> Optional[DirtyEvidenceRange]:
    """Best-effort :func:`mark_evidence_dirty` for evidence producers."""

    if started_at is None or ended_at is None:
        logger.debug("🩹 Skipping dirty mark (%s): no absolute bounds", reason)
        return None
    try:
        return await mark_evidence_dirty(
            user_id,
            started_at,
            ended_at,
            source_revision,
            reason,
            source_kind=source_kind,
        )
    except Exception:
        logger.warning("🩹 Failed to mark evidence dirty (%s)", reason, exc_info=True)
        return None


async def note_conversation_dirty(
    conversation_id: str,
    reason: str,
    *,
    source_revision: Optional[str] = None,
    source_kind: str = "conversation",
) -> Optional[DirtyEvidenceRange]:
    """Mark a conversation's absolute audio interval dirty, best effort.

    The bounds come from the conversation's audio range claims — the authoritative
    wall-clock interval its evidence occupies — falling back to the conversation's own
    semantic bounds when it holds no claim yet.
    """

    try:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if conversation is None:
            return None
        if conversation.memory_space_id and conversation.published_to_main_at is None:
            return None
        ranges = conversation.audio_ranges or []
        if ranges:
            started_at = min(_as_utc(item.started_at) for item in ranges)
            ended_at = max(_as_utc(item.ended_at) for item in ranges)
        else:
            started_at = conversation.started_at
            ended_at = conversation.ended_at
        return await note_evidence_dirty(
            conversation.user_id,
            started_at,
            ended_at,
            source_revision or conversation_id,
            reason,
            source_kind=source_kind,
        )
    except Exception:
        logger.warning(
            "🩹 Failed to mark conversation %s dirty (%s)",
            conversation_id,
            reason,
            exc_info=True,
        )
        return None


# Strong references to fire-and-forget marks, so the event loop cannot drop a task
# that no one awaits.
_pending_marks: set[asyncio.Task] = set()


def schedule_conversation_dirty(conversation_id: str, reason: str) -> None:
    """Schedule :func:`note_conversation_dirty` from synchronous code.

    Every caller of ``start_post_conversation_jobs`` runs inside an event loop (a
    FastAPI handler or an ``@async_job`` worker), so the mark rides that loop. Without
    one there is nothing safe to do — Beanie's client is bound to the loop that
    created it — so this logs and leaves the range to the recovery scan.
    """

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "🩹 No running loop for dirty mark (%s) of %s", reason, conversation_id
        )
        return
    task = loop.create_task(note_conversation_dirty(conversation_id, reason))
    _pending_marks.add(task)
    task.add_done_callback(_pending_marks.discard)
