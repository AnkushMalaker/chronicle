import os
from contextlib import asynccontextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from beanie import init_beanie
from fastapi import BackgroundTasks, HTTPException
from fastapi.encoders import jsonable_encoder
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.memory_audit import MemoryAuditEntry
from backend.models.timeline import (
    DirtyEvidenceRange,
    EvidenceLocator,
    MemoryReviewProposal,
    TimelineAnalysisRun,
    TimelineAudioRange,
    TimelineDay,
    TimelineEpisode,
    TimelineEvidenceRef,
    TimelinePublicationDayPlan,
    TimelinePublicationJournal,
    clip_audio_ranges,
    merge_audio_ranges,
)
from backend.models.user import User
from backend.routers.modules import timeline_routes
from backend.routers.modules.timeline_routes import (
    _episode_payload,
    _refs_overlapping,
    _run_payload,
)
from backend.services.memory.base import DayWriteOutcome
from backend.services.timeline import publication as timeline_publication
from backend.services.timeline import review as timeline_review
from backend.services.timeline.activity_policy import retire_recording_only_episodes
from backend.services.timeline.consolidation import (
    active_semantic_groups,
    snapshot_episodes,
)
from backend.services.timeline.memory import episode_semantic_memory_enabled
from backend.services.timeline.review_projection import build_day_review_projection
from backend.services.timeline.snapshots import snapshot_from_projection


def assert_utc(value: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_run_payload_marks_mongo_naive_datetimes_as_utc():
    value = datetime(2026, 8, 6, 12, 30)
    run = SimpleNamespace(
        run_id="run-one",
        state="complete",
        attempts=1,
        retry_after=None,
        error=None,
        evidence_revision="requested",
        processed_evidence_revision="processed",
        created_at=value,
        completed_at=value,
    )

    payload = jsonable_encoder(_run_payload(run))

    assert_utc(payload["created_at"])
    assert_utc(payload["completed_at"])


def test_episode_payload_marks_episode_and_evidence_datetimes_as_utc():
    value = datetime(2026, 8, 6, 12, 30)
    episode = SimpleNamespace(
        episode_id="episode-one",
        episode_key="key-one",
        revision=1,
        started_at=value,
        ended_at=value.replace(minute=45),
        kind="work",
        title="Work",
        summary="",
        detailed_summary=None,
        status="active",
        confirmed_at=None,
        confirmed_fields=[],
        memory_policy="auto",
        salience="routine",
        confidence=0.9,
        activity_mode="foreground",
        entities=[],
        attributes={},
        assertions=[],
        evidence_refs=[
            TimelineEvidenceRef(
                evidence_id="observation:one",
                kind="observation",
                locator=EvidenceLocator(
                    capture_source_id="screenpipe-test",
                    modality="screen",
                    track_id="display-1",
                ),
                started_at=value,
                ended_at=value.replace(minute=45),
                role="application_state",
            )
        ],
        related_episode_ids=[],
        related_conversation_ids=[],
        audio_ranges=[],
        parent_episode_id=None,
        representative_image=None,
    )

    payload = jsonable_encoder(_episode_payload(episode))

    assert_utc(payload["started_at"])
    assert_utc(payload["ended_at"])
    assert_utc(payload["evidence"][0]["started_at"])
    assert_utc(payload["evidence"][0]["ended_at"])


def test_aware_payload_datetime_is_converted_to_utc():
    offset = timezone(timedelta(hours=5, minutes=30))
    run = SimpleNamespace(
        run_id="run-one",
        state="pending",
        attempts=0,
        retry_after=None,
        error=None,
        evidence_revision="requested",
        processed_evidence_revision=None,
        created_at=datetime(2026, 8, 6, 12, 30, tzinfo=offset),
        completed_at=None,
    )

    payload = jsonable_encoder(_run_payload(run))

    assert payload["created_at"] == "2026-08-06T07:00:00+00:00"


def test_episode_payload_exposes_durable_identity_and_field_confirmation():
    """The UI needs field ownership and durable identity across reanalysis."""

    value = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)
    episode = SimpleNamespace(
        episode_id="episode-one",
        episode_key="durable-key",
        revision=3,
        started_at=value,
        ended_at=value.replace(minute=45),
        kind="gaming_session",
        title="Played with Daksh",
        summary="",
        detailed_summary=None,
        status="settled",
        confirmed_at=value,
        confirmed_fields=["title"],
        memory_policy="remember",
        salience="notable",
        confidence=0.9,
        activity_mode="foreground",
        entities=["Daksh"],
        attributes={},
        assertions=[],
        evidence_refs=[
            TimelineEvidenceRef(
                evidence_id="audio_span:one",
                kind="audio_span",
                locator=EvidenceLocator(
                    capture_source_id="audio-test",
                    modality="audio",
                    track_id="input",
                ),
                started_at=value,
                ended_at=value.replace(minute=45),
                role="uncertain",
                metadata={"conversation_id": "conv-42"},
            )
        ],
        related_episode_ids=[],
        related_conversation_ids=[],
        audio_ranges=[],
        parent_episode_id=None,
        representative_image=None,
    )

    payload = jsonable_encoder(_episode_payload(episode))

    assert payload["episode_key"] == "durable-key"
    assert payload["revision"] == episode.revision
    assert payload["status"] == "settled"
    assert payload["confirmed_fields"] == ["title"]
    assert payload["memory_policy"] == "remember"
    # Without this the episode cannot deep-link into the recording it cites.
    assert payload["evidence"][0]["metadata"]["conversation_id"] == "conv-42"


def _ref(evidence_id: str, start_minute: int, end_minute: int | None):
    base = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    return TimelineEvidenceRef(
        evidence_id=evidence_id,
        kind="observation",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-test",
            modality="screen",
            track_id="display-1",
        ),
        started_at=base + timedelta(minutes=start_minute),
        ended_at=None if end_minute is None else base + timedelta(minutes=end_minute),
        role="application_state",
    )


def test_split_repartitions_evidence_and_shares_only_spanning_refs():
    """Each half must cite what it actually covers.

    Copying every ref to both halves would make a split silently claim the same
    evidence twice; dropping a ref that straddles the cut would lose it entirely.
    """

    base = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    episode = SimpleNamespace(
        evidence_refs=[
            _ref("early", 0, 10),
            _ref("spanning", 5, 25),
            _ref("late", 20, 30),
            _ref("point-late", 22, None),
        ]
    )
    cut = base + timedelta(minutes=15)

    head = {ref.evidence_id for ref in _refs_overlapping(episode, base, cut)}
    tail = {
        ref.evidence_id
        for ref in _refs_overlapping(episode, cut, base + timedelta(minutes=30))
    }

    assert head == {"early", "spanning"}
    assert tail == {"spanning", "late", "point-late"}
    # Nothing is lost across the cut.
    assert head | tail == {"early", "spanning", "late", "point-late"}


# --- audio claims: splitting and merging an episode must move its audio with it ---

EPOCH = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)


def _range(range_id: str, chunk_ids: list[str], start_min: int, end_min: int):
    return TimelineAudioRange(
        range_id=range_id,
        capture_source_id="screenpipe:output",
        time_basis="recorded",
        chunk_ids=chunk_ids,
        started_at=EPOCH + timedelta(minutes=start_min),
        ended_at=EPOCH + timedelta(minutes=end_min),
        source_stream="screenpipe:output",
    )


def _spans(*, minute_of: dict[str, int], duration: float = 10.0):
    return {
        chunk_id: (EPOCH + timedelta(minutes=minute), duration)
        for chunk_id, minute in minute_of.items()
    }


def test_splitting_an_episode_cuts_its_audio_claim_instead_of_copying_it():
    """Both halves used to claim — and play — the whole original recording."""

    ranges = [_range("r1", ["c0", "c30", "c50"], 0, 60)]
    spans = _spans(minute_of={"c0": 0, "c30": 30, "c50": 50})
    cut = EPOCH + timedelta(minutes=40)

    head = clip_audio_ranges(ranges, EPOCH, cut, spans, keep_unplaceable=True)
    tail = clip_audio_ranges(
        ranges, cut, EPOCH + timedelta(minutes=60), spans, keep_unplaceable=False
    )

    assert [item.chunk_ids for item in head] == [["c0", "c30"]]
    assert [item.chunk_ids for item in tail] == [["c50"]]
    # Bounds are clipped to each half, so neither claims past the cut.
    assert head[0].ended_at == cut
    assert tail[0].started_at == cut
    # No chunk is claimed by both halves — that was the defect.
    assert set(head[0].chunk_ids).isdisjoint(tail[0].chunk_ids)


def test_a_range_entirely_on_one_side_of_the_cut_does_not_survive_on_the_other():
    ranges = [_range("early", ["c0"], 0, 10), _range("late", ["c50"], 50, 60)]
    spans = _spans(minute_of={"c0": 0, "c50": 50})
    cut = EPOCH + timedelta(minutes=40)

    head = clip_audio_ranges(ranges, EPOCH, cut, spans, keep_unplaceable=True)
    tail = clip_audio_ranges(
        ranges, cut, EPOCH + timedelta(minutes=60), spans, keep_unplaceable=False
    )

    assert [item.range_id for item in head] == ["early"]
    assert [item.range_id for item in tail] == ["late"]


def test_a_chunk_with_no_capture_time_stays_with_the_head_rather_than_both():
    """3% of this deployment's chunks predate ``captured_at`` and cannot be placed.

    Dropping one loses a reference a later backfill could resolve; keeping it on both
    sides is the double-claim the split fix exists to remove. It stays with the head.
    """

    ranges = [_range("r1", ["c0", "unanchored"], 0, 60)]
    spans = _spans(minute_of={"c0": 0})  # "unanchored" deliberately absent
    cut = EPOCH + timedelta(minutes=40)

    head = clip_audio_ranges(ranges, EPOCH, cut, spans, keep_unplaceable=True)
    tail = clip_audio_ranges(
        ranges, cut, EPOCH + timedelta(minutes=60), spans, keep_unplaceable=False
    )

    assert head[0].chunk_ids == ["c0", "unanchored"]
    assert tail == []


def test_merging_episodes_unions_their_audio_claims():
    """A merged episode used to keep only the survivor's audio."""

    survivor = [_range("r1", ["c0"], 0, 20)]
    absorbed_one = [_range("r2", ["c30"], 30, 40)]
    absorbed_two = [_range("r3", ["c50"], 50, 60)]

    merged = merge_audio_ranges([survivor, absorbed_one, absorbed_two])

    assert [item.range_id for item in merged] == ["r1", "r2", "r3"]
    assert [item.chunk_ids for item in merged] == [["c0"], ["c30"], ["c50"]]


def test_merging_deduplicates_a_range_two_episodes_both_cite():
    shared = _range("shared", ["c0"], 0, 20)

    merged = merge_audio_ranges([[shared], [shared, _range("other", ["c30"], 30, 40)]])

    assert [item.range_id for item in merged] == ["shared", "other"]


def test_merged_claims_come_back_in_wall_clock_order():
    merged = merge_audio_ranges(
        [[_range("late", ["c50"], 50, 60)], [_range("early", ["c0"], 0, 10)]]
    )

    assert [item.range_id for item in merged] == ["early", "late"]


# --- durable identity: split/merge lineage and stable-key resolution ---


@pytest.fixture
async def route_db(mongo_service):
    """Lineage is a property of persisted rows, so these exercise the real routes."""

    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_timeline_routes_db"]
    await init_beanie(
        database=database,
        document_models=[
            timeline_routes.AudioEvidenceSpan,
            TimelineEpisode,
            TimelineDay,
            TimelineAnalysisRun,
            timeline_routes.TimelineReconciliationRequest,
            DirtyEvidenceRange,
            MemoryReviewProposal,
            TimelinePublicationJournal,
            MemoryAuditEntry,
            User,
        ],
    )
    yield database
    await client.drop_database("test_timeline_routes_db")
    client.close()


@pytest.fixture(autouse=True)
def _local_publication_lock(monkeypatch):
    @asynccontextmanager
    async def unlocked(*_args, **_kwargs):
        yield

    monkeypatch.setattr(timeline_publication, "distributed_lock", unlocked)


async def _route_user() -> User:
    user = User(email=f"{os.urandom(4).hex()}@example.com", hashed_password="x")
    await user.insert()
    return user


async def _stored_episode(user: User, *, start_minute: int, end_minute: int, **extra):
    base = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
    payload = {
        "run_id": extra.pop("run_id", "run-one"),
        "user_id": str(user.id),
        "local_date": date(2026, 8, 6),
        "timezone": "Etc/UTC",
        "started_at": base + timedelta(minutes=start_minute),
        "ended_at": base + timedelta(minutes=end_minute),
        "kind": "work",
        "title": extra.pop("title", "Working"),
        "summary": "",
        "confidence": 0.9,
        "activity_mode": "foreground",
    }
    payload.update(extra)
    episode = TimelineEpisode(**payload)
    await episode.insert()
    return episode


@pytest.mark.asyncio
async def test_day_payload_separates_unreconciled_evidence_from_episode_gaps(route_db):
    user = await _route_user()
    day = TimelineDay(
        user_id=str(user.id),
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
        coverage={"unassigned_intervals": []},
    )
    await day.insert()
    await _stored_episode(user, start_minute=0, end_minute=30)
    base = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
    await DirtyEvidenceRange(
        user_id=str(user.id),
        started_at=base,
        ended_at=base + timedelta(minutes=20),
        evidence_revision=2,
        trigger_reasons=["late_observation"],
        not_before=base,
        force_after=base + timedelta(minutes=5),
    ).insert()

    payload = await timeline_routes.get_timeline_day(
        local_date=date(2026, 8, 6), timezone="Etc/UTC", user=user
    )

    assert payload["review"]["state"] == "episodes_pending"
    assert payload["coverage"]["unassigned_intervals"] == []
    assert len(payload["reconciliation"]["ranges"]) == 1
    assert payload["reconciliation"]["ranges"][0]["trigger_reasons"] == [
        "late_observation"
    ]


@pytest.mark.asyncio
async def test_day_payload_exposes_active_episodes_without_a_snapshot(route_db):
    user = await _route_user()
    await TimelineDay(
        user_id=str(user.id),
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
    ).insert()
    episode = await _stored_episode(
        user,
        start_minute=0,
        end_minute=30,
        status="settled",
    )

    payload = await timeline_routes.get_timeline_day(
        local_date=date(2026, 8, 6), timezone="Etc/UTC", user=user
    )

    assert [item["episode_id"] for item in payload["episodes"]] == [episode.episode_id]


@pytest.mark.asyncio
async def test_episode_review_cannot_finalize_while_evidence_is_unreconciled(route_db):
    user = await _route_user()
    day = await _published_day(user)
    base = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
    await DirtyEvidenceRange(
        user_id=str(user.id),
        started_at=base,
        ended_at=base + timedelta(minutes=20),
        evidence_revision=2,
        not_before=base,
        force_after=base + timedelta(minutes=5),
    ).insert()

    with pytest.raises(HTTPException) as raised:
        await timeline_routes.finalize_timeline_episode_review(
            local_date=date(2026, 8, 6),
            body=timeline_routes.ReviewDayRequest(
                timezone="Etc/UTC", snapshot_id=day.current_snapshot_id
            ),
            background_tasks=BackgroundTasks(),
            user=user,
        )

    assert raised.value.status_code == 409
    assert "reconciliation" in raised.value.detail


@pytest.mark.asyncio
async def test_dismissing_terminal_failure_unblocks_exact_snapshot_finalization(
    route_db,
):
    user = await _route_user()
    day = await _published_day(user)
    base = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
    failed = DirtyEvidenceRange(
        user_id=str(user.id),
        started_at=base,
        ended_at=base + timedelta(minutes=20),
        evidence_revision=2,
        not_before=base,
        force_after=base + timedelta(minutes=5),
        state="failed",
        last_error="exhausted interpretation retries",
        rejection_hypothesis_id="hypothesis-1",
        rejection_reason_code="insufficient_context",
        rejection_evidence_ids=["observation:1"],
    )
    await failed.insert()

    with pytest.raises(HTTPException, match="reconciliation"):
        await timeline_routes.finalize_timeline_episode_review(
            local_date=day.local_date,
            body=timeline_routes.ReviewDayRequest(
                timezone=day.timezone, snapshot_id=day.current_snapshot_id
            ),
            background_tasks=BackgroundTasks(),
            user=user,
        )

    payload = await timeline_routes.dismiss_terminal_reconciliation_range(
        dirty_range_id=failed.dirty_range_id,
        body=timeline_routes.DismissFailedRangeRequest(
            reason="Reviewed the evidence; leave this interval unresolved."
        ),
        user=user,
    )
    finalized = await timeline_routes.finalize_timeline_episode_review(
        local_date=day.local_date,
        body=timeline_routes.ReviewDayRequest(
            timezone=day.timezone, snapshot_id=day.current_snapshot_id
        ),
        background_tasks=BackgroundTasks(),
        user=user,
    )

    stored = await DirtyEvidenceRange.get(failed.id)
    assert payload["state"] == "dismissed"
    assert payload["resolution_history"][0]["action"] == "dismissed"
    assert stored.last_error == "exhausted interpretation retries"
    assert stored.rejection_hypothesis_id == "hypothesis-1"
    assert finalized["state"] == "episodes_pending"


@pytest.mark.asyncio
async def test_failed_range_dismissal_is_owner_scoped(route_db):
    owner = await _route_user()
    other = User(
        email="other@example.com",
        display_name="Other",
        hashed_password="hash",
    )
    await other.insert()
    base = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
    failed = DirtyEvidenceRange(
        user_id=str(owner.id),
        started_at=base,
        ended_at=base + timedelta(minutes=20),
        evidence_revision=2,
        not_before=base,
        force_after=base,
        state="failed",
    )
    await failed.insert()

    with pytest.raises(HTTPException) as raised:
        await timeline_routes.dismiss_terminal_reconciliation_range(
            dirty_range_id=failed.dirty_range_id,
            body=timeline_routes.DismissFailedRangeRequest(reason="Dismiss"),
            user=other,
        )

    assert raised.value.status_code == 404
    assert (await DirtyEvidenceRange.get(failed.id)).state == "failed"


@pytest.mark.asyncio
async def test_failed_range_dismissal_rejects_nonterminal_state(route_db):
    user = await _route_user()
    base = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
    pending = DirtyEvidenceRange(
        user_id=str(user.id),
        started_at=base,
        ended_at=base + timedelta(minutes=20),
        evidence_revision=2,
        not_before=base,
        force_after=base,
    )
    await pending.insert()

    with pytest.raises(HTTPException) as raised:
        await timeline_routes.dismiss_terminal_reconciliation_range(
            dirty_range_id=pending.dirty_range_id,
            body=timeline_routes.DismissFailedRangeRequest(reason="Dismiss"),
            user=user,
        )

    assert raised.value.status_code == 409
    assert "terminal failed" in raised.value.detail
    assert (await DirtyEvidenceRange.get(pending.id)).state == "pending"


@pytest.mark.asyncio
async def test_episode_review_confirms_structure_without_queuing_memory(route_db):
    user = await _route_user()
    day = await _published_day(user)

    payload = await timeline_routes.finalize_timeline_episode_review(
        local_date=date(2026, 8, 6),
        body=timeline_routes.ReviewDayRequest(
            timezone="Etc/UTC", snapshot_id=day.current_snapshot_id
        ),
        background_tasks=BackgroundTasks(),
        user=user,
    )
    stored = await TimelineDay.find_one(TimelineDay.user_id == str(user.id))

    assert payload["state"] == "episodes_pending"
    assert stored.review_snapshot_id == day.current_snapshot_id
    assert stored.reviewed_snapshot_id == day.current_snapshot_id
    assert stored.review_state == "episodes_pending"


@pytest.mark.asyncio
async def test_structure_confirmation_publishes_an_exact_provisional_revision(
    route_db, monkeypatch
):
    user = await _route_user()
    started_at = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
    evidence = TimelineEvidenceRef(
        evidence_id="screen:confirmed",
        kind="observation",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-test",
            modality="screen",
            track_id="display-1",
        ),
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=30),
        role="application_state",
    )
    episode = await _stored_episode(
        user,
        start_minute=0,
        end_minute=30,
        conversational=True,
        status="provisional",
        revision=4,
        evidence_refs=[evidence],
    )
    day = await _published_day(user)
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(timeline_routes, "enqueue_episode_detailed_summary", enqueue)

    payload = await timeline_routes.confirm_timeline_episode_structure(
        day.local_date,
        episode.episode_key,
        episode.revision,
        timeline_routes.ReviewDayRequest(
            timezone=day.timezone,
            snapshot_id=day.current_snapshot_id,
        ),
        user,
    )

    successor = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == payload["episode_id"]
    )
    predecessor = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode.episode_id
    )
    refreshed_day = await TimelineDay.get(day.id)
    assert predecessor.status == "superseded"
    assert successor.status == "provisional"
    assert successor.episode_key == episode.episode_key
    assert successor.revision == 5
    assert successor.confirmed_fields == ["ended_at", "evidence_refs", "started_at"]
    assert [item.evidence_id for item in successor.evidence_refs] == [
        "screen:confirmed"
    ]
    assert [
        (item.episode_key, item.revision)
        for item in refreshed_day.current_snapshot.episode_revisions
    ] == [(episode.episode_key, 5)]
    decision = refreshed_day.review_decisions[-1]
    assert decision.action == "episode_structure_confirm"
    assert decision.before["episodes"][0]["revision"] == 4
    assert decision.after["episodes"][0]["revision"] == 5
    assert [
        item["evidence_id"] for item in decision.after["episodes"][0]["evidence_refs"]
    ] == ["screen:confirmed"]
    enqueue.assert_awaited_once()
    enqueued = enqueue.await_args.args[0]
    assert enqueued.episode_id == successor.episode_id
    assert enqueued.episode_key == successor.episode_key
    assert enqueued.revision == successor.revision
    assert enqueued.confirmed_fields == successor.confirmed_fields

    review = await timeline_routes.finalize_timeline_episode_review(
        local_date=refreshed_day.local_date,
        body=timeline_routes.ReviewDayRequest(
            timezone=refreshed_day.timezone,
            snapshot_id=refreshed_day.current_snapshot_id,
        ),
        background_tasks=BackgroundTasks(),
        user=user,
    )
    assert review["state"] == "episodes_pending"


@pytest.mark.asyncio
async def test_structure_confirmation_rejects_a_revision_outside_the_exact_snapshot(
    route_db,
):
    user = await _route_user()
    episode = await _stored_episode(
        user, start_minute=0, end_minute=30, status="provisional", revision=2
    )
    day = await _published_day(user)

    with pytest.raises(HTTPException) as raised:
        await timeline_routes.confirm_timeline_episode_structure(
            day.local_date,
            episode.episode_key,
            1,
            timeline_routes.ReviewDayRequest(
                timezone=day.timezone,
                snapshot_id=day.current_snapshot_id,
            ),
            user,
        )

    assert raised.value.status_code == 409
    assert "no longer in this Timeline snapshot" in raised.value.detail
    assert await TimelineEpisode.find_all().count() == 1


@pytest.mark.asyncio
async def test_a_stable_key_resolves_to_the_current_episode(route_db):
    user = await _route_user()
    episode = await _stored_episode(user, start_minute=0, end_minute=30)
    await _published_day(user)

    payload = await timeline_routes.get_timeline_episode_by_key(
        episode.episode_key, user
    )

    assert payload["resolved"] is True
    assert payload["episode_id"] == episode.episode_id
    assert payload["revision"] == episode.revision
    assert "pipeline" not in payload


@pytest.mark.asyncio
async def test_a_key_that_never_existed_is_a_404(route_db):
    user = await _route_user()

    with pytest.raises(HTTPException) as error:
        await timeline_routes.get_timeline_episode_by_key("no-such-key", user)

    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_an_episode_key_survives_a_new_generation(route_db):
    """Reanalysis replaces the row; the key still names the same event."""

    user = await _route_user()
    old = await _stored_episode(
        user, start_minute=0, end_minute=30, run_id="run-old", title="Old row"
    )
    new = await _stored_episode(
        user,
        start_minute=0,
        end_minute=35,
        run_id="run-new",
        title="Carried forward",
        episode_key=old.episode_key,
        revision=old.revision + 1,
    )
    await _published_day(user, run_id="run-new")

    payload = await timeline_routes.get_timeline_episode_by_key(old.episode_key, user)

    assert payload["episode_id"] == new.episode_id


@pytest.mark.asyncio
async def test_merging_supersedes_every_input_into_a_fresh_exact_lineage(route_db):
    user = await _route_user()
    survivor = await _stored_episode(user, start_minute=0, end_minute=30, revision=3)
    absorbed = await _stored_episode(user, start_minute=30, end_minute=60, revision=7)
    await _published_day(user)

    merged = await timeline_routes.merge_timeline_episodes(
        timeline_routes.EpisodeMerge(
            episode_ids=[survivor.episode_id, absorbed.episode_id]
        ),
        user,
    )

    kept = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == merged["episode_id"]
    )
    assert kept.episode_key not in {survivor.episode_key, absorbed.episode_key}
    assert kept.revision == 1
    assert kept.predecessor_keys == sorted([survivor.episode_key, absorbed.episode_key])
    assert [
        (item.episode_key, item.revision) for item in kept.predecessor_revisions
    ] == [
        (survivor.episode_key, survivor.revision),
        (absorbed.episode_key, absorbed.revision),
    ]
    assert kept.pinned is True

    for predecessor in (survivor, absorbed):
        stale = await TimelineEpisode.find_one(
            TimelineEpisode.episode_id == predecessor.episode_id
        )
        assert stale is not None, "a merged input is history, not garbage"
        assert stale.status == "superseded"
        assert stale.successor_keys == [kept.episode_key]

        payload = await timeline_routes.get_timeline_episode_by_key(
            predecessor.episode_key, user
        )
        assert payload["resolved"] is False
        assert payload["successor_keys"] == [kept.episode_key]

    resolved = await timeline_routes.get_timeline_episode_by_key(kept.episode_key, user)
    assert resolved["predecessor_revisions"] == [
        {"episode_key": survivor.episode_key, "revision": survivor.revision},
        {"episode_key": absorbed.episode_key, "revision": absorbed.revision},
    ]


@pytest.mark.asyncio
async def test_a_merged_away_episode_leaves_the_day_view(route_db):
    user = await _route_user()
    survivor = await _stored_episode(user, start_minute=0, end_minute=30)
    absorbed = await _stored_episode(user, start_minute=30, end_minute=60)
    await _published_day(user)

    merged = await timeline_routes.merge_timeline_episodes(
        timeline_routes.EpisodeMerge(
            episode_ids=[survivor.episode_id, absorbed.episode_id]
        ),
        user,
    )
    day = await timeline_routes.get_timeline_day(date(2026, 8, 6), "Etc/UTC", user)

    assert [item["episode_id"] for item in day["episodes"]] == [merged["episode_id"]]


@pytest.mark.asyncio
async def test_merging_invalidates_the_stored_grouping_proposal(route_db, monkeypatch):
    user = await _route_user()
    survivor = await _stored_episode(user, start_minute=0, end_minute=30)
    absorbed = await _stored_episode(user, start_minute=30, end_minute=60)
    day = await _published_day(user)
    day.consolidation_state = "ready"
    day.consolidation_snapshot_id = day.current_snapshot_id
    day.consolidation_suggestions = [
        {
            "suggestion_id": "stale",
            "episode_ids": [survivor.episode_id, absorbed.episode_id],
        }
    ]
    await day.save()
    monkeypatch.setattr(
        timeline_routes,
        "synthesize_merged_episode_account",
        AsyncMock(
            return_value=SimpleNamespace(title="Merged", summary="Merged account")
        ),
    )

    await timeline_routes.merge_timeline_episodes(
        timeline_routes.EpisodeMerge(
            episode_ids=[survivor.episode_id, absorbed.episode_id]
        ),
        user,
    )

    refreshed = await TimelineDay.get(day.id)
    assert refreshed.consolidation_state == ""
    assert refreshed.consolidation_suggestions == []


@pytest.mark.asyncio
async def test_day_payload_hides_suggestions_with_missing_episode_ids(route_db):
    user = await _route_user()
    active = await _stored_episode(user, start_minute=0, end_minute=30)
    day = await _published_day(user)
    day.consolidation_state = "ready"
    day.consolidation_snapshot_id = day.current_snapshot_id
    day.consolidation_suggestions = [
        {"suggestion_id": "stale", "episode_ids": [active.episode_id, "missing"]}
    ]
    await day.save()

    payload = await timeline_routes.get_timeline_day(date(2026, 8, 6), "Etc/UTC", user)

    assert payload["consolidation"]["state"] == ""
    assert payload["consolidation"]["suggestions"] == []


@pytest.mark.asyncio
async def test_grouping_request_queues_generation_without_waiting(monkeypatch):
    user = SimpleNamespace(id="user-one")
    day = SimpleNamespace(
        id="day-one",
        current_snapshot_id="a" * 64,
        review_state="episodes_pending",
    )
    queued = AsyncMock(
        return_value={
            "state": "queued",
            "snapshot_id": "a" * 64,
            "model": None,
            "suggestions": [],
            "error": None,
            "generated_at": None,
        }
    )
    collection = SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1))
    )
    day_model = SimpleNamespace(
        user_id="user_id",
        local_date="local_date",
        timezone="timezone",
        find_one=AsyncMock(return_value=day),
        get_pymongo_collection=lambda: collection,
    )
    monkeypatch.setattr(timeline_routes, "TimelineDay", day_model)
    monkeypatch.setattr(timeline_routes, "queue_day_consolidation", queued)

    result = await timeline_routes.suggest_timeline_day_consolidation(
        date(2026, 8, 6),
        timeline_routes.ReviewDayRequest(timezone="Etc/UTC", snapshot_id="a" * 64),
        user,
    )

    assert result == {
        "state": "queued",
        "snapshot_id": "a" * 64,
        "model": None,
        "suggestions": [],
        "error": None,
        "generated_at": None,
    }
    queued.assert_awaited_once_with(
        str(user.id), date(2026, 8, 6), "Etc/UTC", day.current_snapshot_id
    )


@pytest.mark.asyncio
async def test_merging_regenerates_the_semantic_account(route_db, monkeypatch):
    """The survivor's narrow title must not describe a newly widened episode."""

    user = await _route_user()
    survivor = await _stored_episode(
        user,
        start_minute=0,
        end_minute=30,
        title="Career discussion",
        summary="They discuss career direction.",
    )
    absorbed = await _stored_episode(
        user,
        start_minute=30,
        end_minute=60,
        title="Product discussion",
        summary="They discuss product strategy.",
    )

    async def synthesize(episodes):
        assert [item.episode_id for item in episodes] == [
            survivor.episode_id,
            absorbed.episode_id,
        ]
        return SimpleNamespace(
            title="Interview about career and product strategy",
            summary="They discuss career direction and product strategy.",
        )

    monkeypatch.setattr(
        timeline_routes, "synthesize_merged_episode_account", synthesize
    )
    await _published_day(user)

    payload = await timeline_routes.merge_timeline_episodes(
        timeline_routes.EpisodeMerge(
            episode_ids=[survivor.episode_id, absorbed.episode_id]
        ),
        user,
    )

    assert payload["title"] == "Interview about career and product strategy"
    assert payload["summary"] == "They discuss career direction and product strategy."
    kept = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == payload["episode_id"]
    )
    assert kept.title == payload["title"]
    assert kept.summary == payload["summary"]


@pytest.mark.asyncio
async def test_existing_merged_episode_can_regenerate_its_account(
    route_db, monkeypatch
):
    user = await _route_user()
    episode = await _stored_episode(
        user,
        start_minute=0,
        end_minute=60,
        title="Stale first segment title",
        summary="This only describes the first segment.",
    )

    async def synthesize(episodes, *, force=False):
        assert [item.episode_id for item in episodes] == [episode.episode_id]
        assert force is True
        return SimpleNamespace(
            title="Complete interview",
            summary="A coherent account derived from all merged evidence.",
        )

    monkeypatch.setattr(
        timeline_routes, "synthesize_merged_episode_account", synthesize
    )
    await _published_day(user)

    payload = await timeline_routes.regenerate_timeline_episode_account(
        episode.episode_id, user
    )

    assert payload["title"] == "Complete interview"
    assert payload["summary"] == "A coherent account derived from all merged evidence."
    assert payload["confirmed_fields"] == ["summary", "title"]


@pytest.mark.asyncio
async def test_splitting_records_lineage_in_both_directions(route_db):
    user = await _route_user()
    episode = await _stored_episode(user, start_minute=0, end_minute=60, revision=4)
    await _published_day(user)
    original_key = episode.episode_key

    result = await timeline_routes.split_timeline_episode(
        episode.episode_id,
        timeline_routes.EpisodeSplit(
            at=datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)
        ),
        user,
    )
    head, tail = result["episodes"]

    assert head["episode_key"] != original_key
    assert tail["episode_key"] != original_key
    assert head["episode_key"] != tail["episode_key"]

    stored_head = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == head["episode_id"]
    )
    stored_tail = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == tail["episode_id"]
    )
    for part in (stored_head, stored_tail):
        assert part.revision == 1
        assert part.predecessor_keys == [original_key]
        assert [
            (item.episode_key, item.revision) for item in part.predecessor_revisions
        ] == [(original_key, episode.revision)]
    stale_parent = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode.episode_id
    )
    assert stale_parent.status == "superseded"
    assert set(stale_parent.successor_keys) == {
        stored_head.episode_key,
        stored_tail.episode_key,
    }

    # The retired identity now offers both fresh halves rather than guessing one.
    payload = await timeline_routes.get_timeline_episode_by_key(original_key, user)
    assert payload["resolved"] is False
    assert set(payload["successor_keys"]) == {
        stored_head.episode_key,
        stored_tail.episode_key,
    }
    resolved_head = await timeline_routes.get_timeline_episode_by_key(
        stored_head.episode_key, user
    )
    assert resolved_head["predecessor_revisions"] == [
        {"episode_key": original_key, "revision": episode.revision}
    ]


@pytest.mark.asyncio
async def test_a_split_key_with_no_active_row_offers_the_choice(route_db):
    """No single right answer, so the lookup returns both halves rather than guessing."""

    user = await _route_user()
    episode = await _stored_episode(user, start_minute=0, end_minute=60)
    await _published_day(user)
    original_key = episode.episode_key

    result = await timeline_routes.split_timeline_episode(
        episode.episode_id,
        timeline_routes.EpisodeSplit(
            at=datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)
        ),
        user,
    )
    payload = await timeline_routes.get_timeline_episode_by_key(original_key, user)

    assert payload["resolved"] is False
    assert set(payload["successor_keys"]) == {
        item["episode_key"] for item in result["episodes"]
    }


@pytest.mark.asyncio
async def test_bounds_edit_creates_a_newly_affected_day_snapshot(route_db):
    user = await _route_user()
    episode = await _stored_episode(user, start_minute=0, end_minute=30)
    first_day = await _published_day(user)

    await timeline_routes.update_timeline_episode(
        episode.episode_id,
        timeline_routes.EpisodeUpdate(
            started_at=datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 7, 0, 30, tzinfo=timezone.utc),
        ),
        user,
    )

    second_day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == date(2026, 8, 7),
        TimelineDay.timezone == "Etc/UTC",
    )
    refreshed_first = await TimelineDay.get(first_day.id)
    assert second_day is not None
    assert second_day.current_snapshot is not None
    assert len(second_day.current_snapshot.episode_revisions) == 1
    assert refreshed_first.current_snapshot_id != first_day.current_snapshot_id


@pytest.mark.asyncio
async def test_nonconsecutive_group_and_tombstone_publish_new_snapshots(
    route_db, monkeypatch
):
    user = await _route_user()
    first = await _stored_episode(user, start_minute=0, end_minute=20)
    await _stored_episode(user, start_minute=20, end_minute=40)
    resumed = await _stored_episode(user, start_minute=40, end_minute=60)
    day = await _published_day(user)
    monkeypatch.setattr(
        "backend.services.timeline.consolidation.synthesize_merged_episode_account",
        AsyncMock(
            return_value=SimpleNamespace(title="Resumed work", summary="One task")
        ),
    )

    payload = await timeline_routes.create_timeline_semantic_group(
        day.local_date,
        timeline_routes.CreateSemanticGroupRequest(
            timezone=day.timezone,
            snapshot_id=day.current_snapshot_id,
            episode_ids=[first.episode_id, resumed.episode_id],
        ),
        user,
    )
    grouped_day = await TimelineDay.get(day.id)

    assert payload["member_revisions"] == [
        {"episode_key": first.episode_key, "revision": first.revision},
        {"episode_key": resumed.episode_key, "revision": resumed.revision},
    ]
    assert "group_id" not in payload
    assert "episode_keys" not in payload
    assert grouped_day.current_snapshot_id != day.current_snapshot_id
    assert len(grouped_day.semantic_group_history) == 1
    assert grouped_day.semantic_group_history[0].status == "active"

    await timeline_routes.delete_timeline_semantic_group(
        day.local_date,
        payload["group_key"],
        timezone=day.timezone,
        snapshot_id=grouped_day.current_snapshot_id,
        user=user,
    )
    removed_day = await TimelineDay.get(day.id)

    assert [item.revision for item in removed_day.semantic_group_history] == [1, 2]
    assert removed_day.semantic_group_history[-1].status == "tombstone"


@pytest.mark.asyncio
async def test_episode_revision_stops_projecting_a_group_with_a_stale_member(
    route_db, monkeypatch
):
    user = await _route_user()
    first = await _stored_episode(user, start_minute=0, end_minute=20)
    resumed = await _stored_episode(user, start_minute=40, end_minute=60)
    day = await _published_day(user)
    monkeypatch.setattr(
        "backend.services.timeline.consolidation.synthesize_merged_episode_account",
        AsyncMock(return_value=SimpleNamespace(title="Resumed", summary="One task")),
    )
    await timeline_routes.create_timeline_semantic_group(
        day.local_date,
        timeline_routes.CreateSemanticGroupRequest(
            timezone=day.timezone,
            snapshot_id=day.current_snapshot_id,
            episode_ids=[first.episode_id, resumed.episode_id],
        ),
        user,
    )

    await timeline_routes.update_timeline_episode(
        first.episode_id,
        timeline_routes.EpisodeUpdate(title="Corrected title"),
        user,
    )
    revised_day = await TimelineDay.get(day.id)

    assert revised_day.current_snapshot.semantic_group_revisions == []
    assert len(revised_day.semantic_group_history) == 1
    assert revised_day.semantic_group_history[0].status == "active"
    assert revised_day.review_decisions[-1].run_id == revised_day.current_snapshot_id


async def _published_day(user: User, run_id: str = "run-one") -> TimelineDay:
    episodes = await TimelineEpisode.find(
        TimelineEpisode.user_id == str(user.id),
        TimelineEpisode.run_id == run_id,
        {"status": {"$ne": "superseded"}},
    ).to_list()
    snapshot = snapshot_from_projection(
        user_id=str(user.id),
        local_date=date(2026, 8, 6),
        timezone_name="Etc/UTC",
        episodes=episodes,
    )
    day = TimelineDay(
        user_id=str(user.id),
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
        snapshot_state="ready",
    )
    await day.insert()
    return day


@pytest.mark.asyncio
async def test_day_recovers_owner_scoped_reconciliation_progress(route_db, monkeypatch):
    user = await _route_user()
    request = timeline_routes.TimelineReconciliationRequest(
        user_id=str(user.id),
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
        state="running",
        reason="user_bypassed_immich",
        job_id="owned-job",
    )
    await request.insert()
    await timeline_routes.TimelineReconciliationRequest(
        user_id="another-user",
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
        state="running",
        reason="user_bypassed_immich",
        job_id="other-job",
    ).insert()
    looked_up = []

    def progress(job_id):
        looked_up.append(job_id)
        return {"stage": "context", "message": "Reading block 2"}

    monkeypatch.setattr(timeline_routes, "read_job_progress", progress)
    payload = await timeline_routes.get_timeline_day(
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
        user=user,
    )
    assert looked_up == ["owned-job"]
    assert payload["latest_reconciliation"]["request_id"] == request.request_id
    assert payload["latest_reconciliation"]["progress"]["stage"] == "context"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_session_structure_confirmation_preserves_group_and_validates_all_members(
    route_db, monkeypatch, stale
):
    user = await _route_user()
    first = await _stored_episode(
        user, start_minute=0, end_minute=2, status="provisional"
    )
    second = await _stored_episode(
        user, start_minute=2, end_minute=70, status="provisional", conversational=True
    )
    untouched = await _stored_episode(
        user, start_minute=80, end_minute=90, status="provisional"
    )
    day = await _published_day(user)
    monkeypatch.setattr(
        "backend.services.timeline.consolidation.synthesize_merged_episode_account",
        AsyncMock(
            return_value=SimpleNamespace(
                title="Meeting and setup", summary="One meeting"
            )
        ),
    )
    monkeypatch.setattr(
        timeline_routes,
        "enqueue_episode_detailed_summary",
        AsyncMock(return_value=True),
    )
    await timeline_routes.create_timeline_semantic_group(
        day.local_date,
        timeline_routes.CreateSemanticGroupRequest(
            timezone=day.timezone,
            snapshot_id=day.current_snapshot_id,
            episode_ids=[first.episode_id, second.episode_id],
        ),
        user,
    )
    day = await TimelineDay.get(day.id)
    request = timeline_routes.ConfirmSessionStructuresRequest(
        timezone=day.timezone,
        snapshot_id=day.current_snapshot_id,
        episodes=[
            {"episode_key": first.episode_key, "revision": first.revision},
            {
                "episode_key": second.episode_key,
                "revision": second.revision + int(stale),
            },
        ],
    )
    if stale:
        with pytest.raises(HTTPException) as exc:
            await timeline_routes.confirm_timeline_session_structures(
                day.local_date, request, user
            )
        assert exc.value.status_code == 409
        assert (await TimelineEpisode.get(first.id)).status == "provisional"
        assert (
            await TimelineDay.get(day.id)
        ).current_snapshot_id == day.current_snapshot_id
        return
    response = await timeline_routes.confirm_timeline_session_structures(
        day.local_date, request, user
    )
    revised = await TimelineDay.get(day.id)
    assert len(response["episodes"]) == 2
    assert (await TimelineEpisode.get(untouched.id)).confirmed_fields == []
    assert revised.review_state == "episodes_pending"
    assert revised.current_snapshot.semantic_group_revisions[0].revision == 2
    group = revised.semantic_group_history[-1]
    assert group.title == "Meeting and setup"
    assert set(group.episode_ids) == {
        item["episode_id"] for item in response["episodes"]
    }
    assert {(ref.episode_key, ref.revision) for ref in group.member_revisions} == {
        (first.episode_key, first.revision + 1),
        (second.episode_key, second.revision + 1),
    }
    assert len(revised.review_decisions[-1].after["episodes"]) == 2
    for old in [first, second]:
        assert (await TimelineEpisode.get(old.id)).successor_keys == [old.episode_key]
    projected = build_day_review_projection(
        await snapshot_episodes(revised),
        semantic_group_revisions=active_semantic_groups(revised),
        local_date=revised.local_date,
        timezone_name=revised.timezone,
    )
    assert any(
        item["semantic"] and item["episode_count"] == 2 for item in projected["groups"]
    )


@pytest.mark.asyncio
async def test_not_activity_rejects_stale_review_without_removing_episode(route_db):
    user = await _route_user()
    episode = await _stored_episode(user, start_minute=0, end_minute=10)
    await _published_day(user)
    with pytest.raises(HTTPException) as exc:
        await timeline_routes.reject_timeline_activity(
            episode.episode_id,
            timeline_routes.NotActivityRequest(
                local_date=episode.local_date,
                timezone=episode.timezone,
                snapshot_id="stale",
                revision=episode.revision,
            ),
            user,
        )
    assert exc.value.status_code == 409
    assert (await TimelineEpisode.get(episode.id)).status != "superseded"


@pytest.mark.asyncio
async def test_recording_coverage_is_not_a_reviewable_activity(route_db):
    user = await _route_user()
    ref = _ref("audio:test", 0, 10)
    ref.kind = "audio_span"
    ref.metadata = {"state": "no_speech"}
    episode = await _stored_episode(
        user, start_minute=0, end_minute=10, kind="ambient_audio", evidence_refs=[ref]
    )
    real = await _stored_episode(
        user, start_minute=0, end_minute=10, evidence_refs=[_ref("screen:test", 0, 10)]
    )
    day = await _published_day(user)
    payload = await timeline_routes.get_timeline_day(day.local_date, day.timezone, user)
    assert [e["episode_id"] for e in payload["episodes"]] == [real.episode_id]
    assert not episode_semantic_memory_enabled(episode)
    assert await retire_recording_only_episodes(day) == [episode.episode_id]
    assert (await TimelineEpisode.get(episode.id)).status == "superseded"
    assert (await TimelineEpisode.get(real.id)).status != "superseded"


@pytest.mark.asyncio
async def test_not_activity_preserves_remaining_accepted_group(route_db, monkeypatch):
    user = await _route_user()
    members = [
        await _stored_episode(user, start_minute=i * 10, end_minute=(i + 1) * 10)
        for i in range(3)
    ]
    day = await _published_day(user)
    monkeypatch.setattr(
        "backend.services.timeline.consolidation.synthesize_merged_episode_account",
        AsyncMock(
            return_value=SimpleNamespace(title="Work session", summary="Related work")
        ),
    )
    await timeline_routes.create_timeline_semantic_group(
        day.local_date,
        timeline_routes.CreateSemanticGroupRequest(
            timezone=day.timezone,
            snapshot_id=day.current_snapshot_id,
            episode_ids=[e.episode_id for e in members],
        ),
        user,
    )
    day = await TimelineDay.get(day.id)
    await timeline_routes.reject_timeline_activity(
        members[0].episode_id,
        timeline_routes.NotActivityRequest(
            local_date=day.local_date,
            timezone=day.timezone,
            snapshot_id=day.current_snapshot_id,
            revision=members[0].revision,
        ),
        user,
    )
    day = await TimelineDay.get(day.id)
    groups = timeline_routes.active_semantic_groups(day)
    assert len(groups) == 1
    assert groups[0].episode_ids == [e.episode_id for e in members[1:]]
    assert groups[0].revision == 2
    assert groups[0].started_at.replace(tzinfo=timezone.utc) == members[1].started_at


async def test_media_and_empty_input_stay_reference_without_blocking_review(
    route_db, cached_proposal
):
    user = await _route_user()
    media = _ref("tv:dialogue", 0, 10)
    media.kind = "transcript"
    media.role = "media_content"
    media.excerpt = "Dialogue from the television."
    microphone = _ref("mic:empty", 1, 1)
    microphone.kind = "transcript"
    microphone.role = "uncertain"
    microphone.excerpt = None
    episode = await _stored_episode(
        user,
        start_minute=0,
        end_minute=10,
        status="provisional",
        title="Media playback and audio input",
        kind="work_session",
        evidence_refs=[media, microphone],
    )
    other = await _stored_episode(
        user,
        start_minute=1,
        end_minute=10,
        status="provisional",
        evidence_refs=[microphone],
    )
    day = await _published_day(user)
    day.consolidation_state = "ready"
    day.consolidation_snapshot_id = day.current_snapshot_id
    day.consolidation_suggestions = (
        [
            {
                "suggestion_id": "old-media-group",
                "episode_ids": [episode.episode_id, other.episode_id],
                "member_revisions": [
                    {"episode_key": e.episode_key, "revision": e.revision}
                    for e in [episode, other]
                ],
                "source_snapshot_id": day.current_snapshot_id,
                "title": "Watching TV",
                "reason": "The same show",
                "confidence": 0.95,
            }
        ]
        if cached_proposal
        else []
    )
    await day.save()
    payload = await timeline_routes.get_timeline_day(day.local_date, day.timezone, user)
    reference = payload["episodes"][0]
    assert reference["episode_id"] == episode.episode_id
    assert reference["requires_activity_review"] is False
    assert reference["memory_eligible"] is False
    assert payload["review_projection"]["groups"][0]["lane"] == "background"
    assert payload["consolidation"]["suggestions"] == []
    assert payload["consolidation"]["state"] == ("" if cached_proposal else "ready")
    with pytest.raises(HTTPException) as cached:
        await timeline_routes.resolve_timeline_day_consolidation(
            day.local_date,
            timeline_routes.ResolveConsolidationRequest(
                timezone=day.timezone,
                snapshot_id=day.current_snapshot_id,
                accepted_suggestion_ids=["old-media-group"],
                finalize=False,
            ),
            user,
        )
    assert cached.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await timeline_routes.confirm_timeline_episode_structure(
            day.local_date,
            episode.episode_key,
            episode.revision,
            timeline_routes.ReviewDayRequest(
                timezone=day.timezone,
                snapshot_id=day.current_snapshot_id,
            ),
            user,
        )
    assert exc.value.status_code == 422
    assert (await TimelineEpisode.get(episode.id)).revision == episode.revision
    await timeline_routes.finalize_timeline_episode_review(
        local_date=day.local_date,
        body=timeline_routes.ReviewDayRequest(
            timezone=day.timezone,
            snapshot_id=day.current_snapshot_id,
        ),
        background_tasks=BackgroundTasks(),
        user=user,
    )
    assert (
        await TimelineDay.get(day.id)
    ).reviewed_snapshot_id == day.current_snapshot_id


async def test_grouping_acceptance_returns_actionable_prepublication_failure(
    monkeypatch,
):
    day = SimpleNamespace(current_snapshot_id="a" * 64)
    model = SimpleNamespace(
        user_id="user_id",
        local_date="date",
        timezone="timezone",
        find_one=AsyncMock(return_value=day),
    )
    monkeypatch.setattr(timeline_routes, "TimelineDay", model)
    message = "Could not prepare the grouping summaries. No groupings were saved."
    monkeypatch.setattr(
        timeline_routes,
        "resolve_day_consolidation",
        AsyncMock(side_effect=timeline_routes.ConsolidationSynthesisError(message)),
    )
    with pytest.raises(HTTPException) as error:
        await timeline_routes.resolve_timeline_day_consolidation(
            date(2026, 9, 4),
            timeline_routes.ResolveConsolidationRequest(
                timezone="Asia/Kolkata",
                snapshot_id="a" * 64,
                accepted_suggestion_ids=[f"group:{i}" for i in range(8)],
            ),
            SimpleNamespace(id="owner"),
        )
    assert error.value.status_code == 503
    assert error.value.detail == message
