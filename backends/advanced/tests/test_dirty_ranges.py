"""Dirty-range scheduling: coalescing algebra, leasing, and the recovery scan."""

import os
from datetime import datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.timeline import DirtyEvidenceRange
from advanced_omi_backend.services.timeline import dirty_ranges
from advanced_omi_backend.services.timeline.dirty_ranges import _as_utc

START = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class _RedisCounterFake:
    """Stands in for the per-user Redis INCR counter."""

    def __init__(self):
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def dirty_range_documents(mongo_service, monkeypatch):
    """Real documents so the model's validators run, with Redis stubbed."""

    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_dirty_ranges_db"]
    await init_beanie(database=database, document_models=[DirtyEvidenceRange])
    await DirtyEvidenceRange.delete_all()
    counter = _RedisCounterFake()
    monkeypatch.setattr(dirty_ranges, "create_async_redis", lambda **_: counter)
    yield counter
    await client.drop_database("test_dirty_ranges_db")
    client.close()


async def _mark(offset_minutes: float, minutes: float, reason: str, **kwargs):
    return await dirty_ranges.mark_evidence_dirty(
        "user",
        START + timedelta(minutes=offset_minutes),
        START + timedelta(minutes=offset_minutes + minutes),
        f"rev-{reason}-{offset_minutes}",
        reason,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_overlapping_triggers_coalesce_into_one_row(dirty_range_documents):
    first = await _mark(0, 10, "conversation_closed", source_kind="conversation")
    second = await _mark(5, 10, "transcript_revision", source_kind="transcript")

    assert second.dirty_range_id == first.dirty_range_id
    assert await DirtyEvidenceRange.find_all().count() == 1
    # MongoDB returns naive UTC, so compare on the normalized instant.
    assert _as_utc(second.started_at) == START
    assert _as_utc(second.ended_at) == START + timedelta(minutes=15)
    assert second.trigger_reasons == ["conversation_closed", "transcript_revision"]
    assert set(second.source_revisions) == {"conversation", "transcript"}
    assert second.evidence_revision == 2


@pytest.mark.asyncio
async def test_nearby_ranges_within_the_gap_merge(dirty_range_documents):
    first = await _mark(0, 5, "conversation_closed")
    # Starts 3 minutes after the first ends — inside COALESCE_GAP_MINUTES.
    second = await _mark(8, 5, "evidence_span")

    assert second.dirty_range_id == first.dirty_range_id
    assert _as_utc(second.ended_at) == START + timedelta(minutes=13)


@pytest.mark.asyncio
async def test_distant_ranges_stay_separate(dirty_range_documents):
    first = await _mark(0, 5, "conversation_closed")
    second = await _mark(60, 5, "conversation_closed")

    assert second.dirty_range_id != first.dirty_range_id
    assert await DirtyEvidenceRange.find_all().count() == 2


@pytest.mark.asyncio
async def test_debounce_resets_but_forced_deadline_is_preserved(dirty_range_documents):
    first = await _mark(0, 10, "conversation_closed")
    original_force_after = first.force_after
    original_not_before = first.not_before

    second = await _mark(1, 10, "transcript_revision")

    assert _as_utc(second.not_before) >= _as_utc(original_not_before)
    # Late evidence extends the debounce; it must not postpone forced progress.
    drift = abs(_as_utc(second.force_after) - _as_utc(original_force_after))
    assert drift < timedelta(milliseconds=2)  # BSON truncates to milliseconds


@pytest.mark.asyncio
async def test_force_override_schedules_immediately(dirty_range_documents):
    now = datetime.now(timezone.utc)
    row = await _mark(0, 10, "manual_request", not_before=now)
    assert _as_utc(row.not_before) <= now + timedelta(seconds=1)

    due = await dirty_ranges.due_ranges()
    assert [item.dirty_range_id for item in due] == [row.dirty_range_id]


@pytest.mark.asyncio
async def test_trigger_never_coalesces_into_a_leased_range(dirty_range_documents):
    leased_row = await _mark(
        0, 10, "conversation_closed", not_before=datetime.now(timezone.utc)
    )
    leased = await dirty_ranges.lease_due_range("worker-1")
    assert leased is not None and leased.dirty_range_id == leased_row.dirty_range_id
    snapshot = leased.leased_evidence_revision

    fresh = await _mark(2, 10, "transcript_revision")

    assert fresh.dirty_range_id != leased.dirty_range_id
    assert fresh.state == "pending"
    reloaded = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == leased.dirty_range_id
    )
    # The run keeps reconciling its own snapshot; the fresh row re-reconciles after.
    assert reloaded.state == "leased"
    assert reloaded.leased_evidence_revision == snapshot
    assert reloaded.lease_owner == "worker-1"


@pytest.mark.asyncio
async def test_overlapping_trigger_wakes_a_waiting_range(dirty_range_documents):
    row = await _mark(
        0, 10, "conversation_closed", not_before=datetime.now(timezone.utc)
    )
    leased = await dirty_ranges.lease_due_range("worker-1")
    await dirty_ranges.park_waiting(leased, "needs future evidence")

    parked = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == row.dirty_range_id
    )
    assert parked.state == "waiting"
    assert "waiting:needs future evidence" in parked.trigger_reasons

    woken = await _mark(1, 5, "speaker_revision")
    assert woken.dirty_range_id == row.dirty_range_id
    assert woken.state == "pending"


@pytest.mark.asyncio
async def test_lease_is_exclusive_and_expired_leases_are_reclaimed(
    dirty_range_documents,
):
    await _mark(0, 10, "conversation_closed", not_before=datetime.now(timezone.utc))

    first = await dirty_ranges.lease_due_range("worker-1")
    assert first is not None
    assert await dirty_ranges.lease_due_range("worker-2") is None

    reclaimed = await dirty_ranges.reap_expired_leases(
        datetime.now(timezone.utc) + timedelta(minutes=dirty_ranges.LEASE_MINUTES + 1)
    )
    assert reclaimed == 1

    second = await dirty_ranges.lease_due_range("worker-2")
    assert second is not None
    assert second.dirty_range_id == first.dirty_range_id
    assert second.lease_owner == "worker-2"
    assert second.attempts == 2


@pytest.mark.asyncio
async def test_a_range_that_burns_every_attempt_fails(dirty_range_documents):
    await _mark(0, 10, "conversation_closed", not_before=datetime.now(timezone.utc))

    for attempt in range(dirty_ranges.MAX_ATTEMPTS):
        leased = await dirty_ranges.lease_due_range(f"worker-{attempt}")
        assert leased is not None
        # A crashed worker leaves the lease behind; reclaim it and try again.
        await dirty_ranges.reap_expired_leases(
            datetime.now(timezone.utc)
            + timedelta(minutes=dirty_ranges.LEASE_MINUTES + 1)
        )

    assert await dirty_ranges.lease_due_range("worker-last") is None
    row = await DirtyEvidenceRange.find_one({})
    assert row.state == "failed"
    assert "exhausted" in (row.last_error or "")


@pytest.mark.asyncio
async def test_complete_range_terminates_and_records_error(dirty_range_documents):
    await _mark(0, 10, "conversation_closed", not_before=datetime.now(timezone.utc))
    leased = await dirty_ranges.lease_due_range("worker-1")

    await dirty_ranges.complete_range(leased, error="boom")
    row = await DirtyEvidenceRange.find_one({})
    assert row.state == "failed"
    assert row.last_error == "boom"
    assert row.lease_owner is None


@pytest.mark.asyncio
async def test_debounced_range_is_not_due_yet(dirty_range_documents):
    await _mark(0, 10, "conversation_closed")
    assert await dirty_ranges.due_ranges() == []
    assert await dirty_ranges.lease_due_range("worker-1") is None


@pytest.mark.asyncio
async def test_reconcile_dirty_ranges_scan_enqueues_due_work(
    dirty_range_documents, monkeypatch
):
    """The registered cron entry point itself, with only the enqueue faked."""

    # Lease the stale range first, while it is the only due row, so the reclaim below
    # is unambiguous.
    stale = await _mark(240, 10, "evidence_span", not_before=datetime.now(timezone.utc))
    leased = await dirty_ranges.lease_due_range("dead-worker")
    assert leased.dirty_range_id == stale.dirty_range_id

    row = await _mark(
        0, 10, "conversation_closed", not_before=datetime.now(timezone.utc)
    )
    await _mark(120, 10, "conversation_closed")  # still debounced
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    await DirtyEvidenceRange.get_pymongo_collection().update_one(
        {"dirty_range_id": stale.dirty_range_id},
        {"$set": {"lease_expires_at": expired}},
    )

    enqueued: list[str] = []

    def _fake_enqueue(dirty_range_id: str):
        enqueued.append(dirty_range_id)
        return f"job-{dirty_range_id}"

    monkeypatch.setattr(
        "advanced_omi_backend.controllers.queue_controller."
        "enqueue_dirty_range_reconciliation",
        _fake_enqueue,
    )
    recovery_calls = 0

    async def _fake_dispatch_recovery():
        nonlocal recovery_calls
        recovery_calls += 1
        return {"unlatched": 3, "dispatched": 1}

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.dispatch.dispatch_ready_episodes",
        _fake_dispatch_recovery,
    )

    outcome = await dirty_ranges.reconcile_dirty_ranges()

    assert outcome["reclaimed"] == 1
    assert outcome["due"] == 2
    assert outcome["enqueued"] == 2
    assert outcome["dispatched"] == 1
    assert recovery_calls == 1
    assert set(enqueued) == {row.dirty_range_id, stale.dirty_range_id}
