import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.timeline import (
    TimelineAnalysisRun,
    TimelineAudioRange,
    TimelineDay,
    TimelineEpisode,
    TimelineEvidenceRef,
    clip_audio_ranges,
    merge_audio_ranges,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.routers.modules import timeline_routes
from advanced_omi_backend.routers.modules.timeline_routes import (
    _episode_payload,
    _refs_overlapping,
    _run_payload,
)


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
        started_at=value,
        ended_at=value.replace(minute=45),
        kind="work",
        title="Work",
        summary="",
        status="active",
        confirmed_at=None,
        confirmed_fields=[],
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


def test_episode_payload_exposes_durable_identity_and_confirmation():
    """The UI needs these to show a confirmed badge and to survive reanalysis."""

    value = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)
    episode = SimpleNamespace(
        episode_id="episode-one",
        episode_key="durable-key",
        started_at=value,
        ended_at=value.replace(minute=45),
        kind="gaming_session",
        title="Played with Daksh",
        summary="",
        status="confirmed",
        confirmed_at=value,
        confirmed_fields=["title"],
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
    assert payload["status"] == "confirmed"
    assert payload["confirmed_fields"] == ["title"]
    # Without this the episode cannot deep-link into the recording it cites.
    assert payload["evidence"][0]["metadata"]["conversation_id"] == "conv-42"


def _ref(evidence_id: str, start_minute: int, end_minute: int | None):
    base = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    return TimelineEvidenceRef(
        evidence_id=evidence_id,
        kind="observation",
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
        document_models=[TimelineEpisode, TimelineDay, TimelineAnalysisRun, User],
    )
    yield database
    await client.drop_database("test_timeline_routes_db")
    client.close()


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


async def _published_day(user: User, run_id: str = "run-one") -> TimelineDay:
    day = TimelineDay(
        user_id=str(user.id),
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
        active_run_id=run_id,
    )
    await day.insert()
    return day


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
    assert payload["pipeline"] == "day"


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
    )
    await _published_day(user, run_id="run-new")

    payload = await timeline_routes.get_timeline_episode_by_key(old.episode_key, user)

    assert payload["episode_id"] == new.episode_id


@pytest.mark.asyncio
async def test_merging_supersedes_the_absorbed_rows_instead_of_deleting_them(route_db):
    user = await _route_user()
    survivor = await _stored_episode(user, start_minute=0, end_minute=30)
    absorbed = await _stored_episode(user, start_minute=30, end_minute=60)
    await _published_day(user)

    await timeline_routes.merge_timeline_episodes(
        timeline_routes.EpisodeMerge(
            episode_ids=[survivor.episode_id, absorbed.episode_id]
        ),
        user,
    )

    stale = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == absorbed.episode_id
    )
    assert stale is not None, "an absorbed episode is history, not garbage"
    assert stale.status == "superseded"
    assert stale.successor_keys == [survivor.episode_key]

    kept = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == survivor.episode_id
    )
    assert absorbed.episode_key in kept.predecessor_keys
    assert kept.pinned is True

    # A link to the absorbed episode now leads to the survivor that covers it.
    payload = await timeline_routes.get_timeline_episode_by_key(
        absorbed.episode_key, user
    )
    assert payload["resolved"] is False
    assert payload["successor_keys"] == [survivor.episode_key]


@pytest.mark.asyncio
async def test_a_merged_away_episode_leaves_the_day_view(route_db):
    user = await _route_user()
    survivor = await _stored_episode(user, start_minute=0, end_minute=30)
    absorbed = await _stored_episode(user, start_minute=30, end_minute=60)
    await _published_day(user)

    await timeline_routes.merge_timeline_episodes(
        timeline_routes.EpisodeMerge(
            episode_ids=[survivor.episode_id, absorbed.episode_id]
        ),
        user,
    )
    day = await timeline_routes.get_timeline_day(date(2026, 8, 6), "Etc/UTC", user)

    assert [item["episode_id"] for item in day["episodes"]] == [survivor.episode_id]


@pytest.mark.asyncio
async def test_splitting_records_lineage_in_both_directions(route_db):
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
    head, tail = result["episodes"]

    assert head["episode_key"] == original_key
    assert tail["episode_key"] != original_key

    stored_head = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == head["episode_id"]
    )
    stored_tail = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == tail["episode_id"]
    )
    assert stored_tail.predecessor_keys == [original_key]
    assert stored_head.successor_keys == [stored_tail.episode_key]

    # While the head is active the original key still answers with it.
    payload = await timeline_routes.get_timeline_episode_by_key(original_key, user)
    assert payload["resolved"] is True
    assert payload["episode_id"] == stored_head.episode_id


@pytest.mark.asyncio
async def test_a_split_key_with_no_active_row_offers_the_choice(route_db):
    """No single right answer, so the lookup returns both halves rather than guessing."""

    user = await _route_user()
    episode = await _stored_episode(user, start_minute=0, end_minute=60)
    await _published_day(user)
    original_key = episode.episode_key

    await timeline_routes.split_timeline_episode(
        episode.episode_id,
        timeline_routes.EpisodeSplit(
            at=datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)
        ),
        user,
    )
    head = await TimelineEpisode.find_one(TimelineEpisode.episode_key == original_key)
    successor = await _stored_episode(user, start_minute=0, end_minute=20)
    head.status = "superseded"
    head.successor_keys = sorted({*head.successor_keys, successor.episode_key})
    await head.save()

    payload = await timeline_routes.get_timeline_episode_by_key(original_key, user)

    assert payload["resolved"] is False
    assert len(payload["successor_keys"]) == 2
