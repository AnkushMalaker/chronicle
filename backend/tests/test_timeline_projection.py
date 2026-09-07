"""Incremental day projection: which days an episode touches, and what they render."""

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.timeline import (
    EpisodeRevisionRef,
    GroupRevisionRef,
    TimelineDay,
    TimelineEpisode,
    TimelinePublicationJournal,
    TimelineSemanticGroupRevision,
)
from backend.models.user import User
from backend.services.memory import vault_manager
from backend.services.timeline import projection
from backend.services.timeline.projection import (
    affected_local_dates,
    refresh_day_projection,
    refresh_projections,
)
from backend.services.timeline.snapshots import build_day_snapshot
from backend.services.timeline.timezone import canonical_timezone

DB_NAME = "test_timeline_projection_db"
# Stored rows carry canonical zone ids, which is what the projection queries with.
UTC = canonical_timezone("UTC")


def test_an_episode_inside_one_day_affects_only_that_day():
    started = datetime(2026, 8, 6, 14, tzinfo=timezone.utc)
    dates = affected_local_dates(started, started + timedelta(hours=1), "UTC")
    assert dates == [date(2026, 8, 6)]


def test_an_episode_crossing_midnight_affects_both_days():
    """The invariant a single ``local_date`` column cannot express."""

    started = datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc)
    dates = affected_local_dates(started, started + timedelta(hours=1), "UTC")
    assert dates == [date(2026, 8, 6), date(2026, 8, 7)]


def test_local_dates_follow_the_zone_not_utc():
    """22:00–23:00 UTC is already the next local day in Kolkata."""

    started = datetime(2026, 8, 6, 22, tzinfo=timezone.utc)
    dates = affected_local_dates(started, started + timedelta(hours=1), "Asia/Kolkata")
    assert dates == [date(2026, 8, 7)]


def test_a_dst_day_is_bounded_by_its_own_offsets():
    """US DST ends 2026-11-01: that local day is 25 hours long.

    An episode late on the long day must not leak into the next date, which it would
    if day bounds were computed with a single fixed offset.
    """

    started = datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc)  # 23:30 EDT->EST day
    dates = affected_local_dates(
        started, started + timedelta(minutes=15), "America/New_York"
    )
    assert dates == [date(2026, 11, 1)]

    spanning = affected_local_dates(
        datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc),
        datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc),
        "America/New_York",
    )
    assert spanning == [date(2026, 11, 1), date(2026, 11, 2)]


def test_a_naive_timestamp_is_read_as_utc():
    """Mongo hands back naive datetimes; they are not local wall clock."""

    assert affected_local_dates(
        datetime(2026, 8, 6, 14), datetime(2026, 8, 6, 15), "UTC"
    ) == [date(2026, 8, 6)]


@pytest.fixture
async def projection_db(mongo_service, redis_service, tmp_path, monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client[DB_NAME]
    await init_beanie(
        database=database,
        document_models=[
            TimelineEpisode,
            TimelineDay,
            TimelinePublicationJournal,
            User,
        ],
    )
    monkeypatch.setattr(vault_manager, "_DEFAULT_BASE_DIR", tmp_path)
    yield tmp_path
    await client.drop_database(DB_NAME)
    client.close()


async def _user() -> User:
    user = User(
        email=f"timeline-{os.urandom(4).hex()}@example.com",
        hashed_password="x",
    )
    await user.insert()
    return user


async def _episode(user: User, *, start: datetime, end: datetime, **overrides):
    payload = {
        "run_id": overrides.pop("run_id", "run-one"),
        "user_id": str(user.id),
        "local_date": start.date(),
        "timezone": UTC,
        "started_at": start,
        "ended_at": end,
        "kind": "work",
        "title": overrides.pop("title", "Working"),
        "summary": "",
        "confidence": 0.9,
        "activity_mode": "foreground",
    }
    payload.update(overrides)
    episode = TimelineEpisode(**payload)
    await episode.insert()
    return episode


def _index(vault_root, user: User, local_date: date) -> str:
    note = vault_root / str(user.id) / "Daily" / f"{local_date.isoformat()}.md"
    return note.read_text(encoding="utf-8") if note.is_file() else ""


async def _snapshot_episode_keys(user: User, local_date: date) -> list[str]:
    day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == UTC,
    )
    assert day is not None and day.current_snapshot is not None
    return [item.episode_key for item in day.current_snapshot.episode_revisions]


@pytest.mark.asyncio
async def test_a_cross_midnight_episode_is_projected_into_both_days(projection_db):
    user = await _user()
    await _episode(
        user,
        start=datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc),
        end=datetime(2026, 8, 7, 0, 30, tzinfo=timezone.utc),
        title="Late session",
    )

    for local_date in (date(2026, 8, 6), date(2026, 8, 7)):
        assert await refresh_day_projection(str(user.id), local_date, "UTC") is True
        assert len(await _snapshot_episode_keys(user, local_date)) == 1
        assert _index(projection_db, user, local_date) == ""


@pytest.mark.asyncio
async def test_replaying_an_unchanged_projection_reports_no_change(projection_db):
    user = await _user()
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
    )

    assert await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC") is True
    before = await _snapshot_episode_keys(user, date(2026, 8, 6))
    assert await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC") is False
    assert await _snapshot_episode_keys(user, date(2026, 8, 6)) == before
    assert _index(projection_db, user, date(2026, 8, 6)) == ""


@pytest.mark.asyncio
async def test_projection_preserves_exact_group_revisions_from_current_snapshot(
    projection_db,
):
    user = await _user()
    first = await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
        title="First",
    )
    second = await _episode(
        user,
        start=datetime(2026, 8, 6, 11, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
        title="Second",
    )
    local_date = date(2026, 8, 6)
    await refresh_day_projection(str(user.id), local_date, "UTC")
    day = await TimelineDay.find_one(TimelineDay.user_id == str(user.id))
    group = TimelineSemanticGroupRevision(
        group_key="group-1",
        revision=1,
        member_revisions=[
            EpisodeRevisionRef(episode_key=first.episode_key, revision=first.revision),
            EpisodeRevisionRef(
                episode_key=second.episode_key, revision=second.revision
            ),
        ],
        episode_ids=[first.episode_id, second.episode_id],
        source_snapshot_id=day.current_snapshot_id,
        title="Related work",
        summary="Two parts of one effort.",
        started_at=first.started_at,
        ended_at=second.ended_at,
    )
    group_ref = GroupRevisionRef(
        owner_local_date=local_date,
        group_key=group.group_key,
        revision=group.revision,
    )
    day.semantic_group_history = [group]
    day.current_snapshot = build_day_snapshot(
        user_id=str(user.id),
        local_date=local_date,
        timezone_name=UTC,
        evidence_state_hash=day.current_snapshot.evidence_state_hash,
        episode_revisions=day.current_snapshot.episode_revisions,
        semantic_group_revisions=[group_ref],
    )
    day.current_snapshot_id = day.current_snapshot.snapshot_id
    await day.save()

    assert await refresh_day_projection(str(user.id), local_date, "UTC") is False
    reloaded = await TimelineDay.get(day.id)
    assert reloaded.current_snapshot.semantic_group_revisions == [group_ref]


@pytest.mark.asyncio
async def test_a_superseded_revision_is_not_projected(projection_db):
    user = await _user()
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
        title="Old bounds",
        status="superseded",
    )
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 11, tzinfo=timezone.utc),
        title="New bounds",
        revision=1,
    )

    await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC")
    keys = await _snapshot_episode_keys(user, date(2026, 8, 6))
    assert len(keys) == 1


@pytest.mark.asyncio
async def test_projection_uses_active_revisions_without_a_run_pointer(projection_db):
    user = await _user()
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
    )

    await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC")

    day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == date(2026, 8, 6),
        TimelineDay.timezone == UTC,
    )
    assert day is not None
    assert day.current_snapshot_id is not None


@pytest.mark.asyncio
async def test_the_publish_hook_refreshes_every_day_an_episode_touches(projection_db):
    user = await _user()
    episode = await _episode(
        user,
        start=datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc),
        end=datetime(2026, 8, 7, 0, 30, tzinfo=timezone.utc),
        title="Overnight",
    )

    changed = await refresh_projections(str(user.id), episode=episode)

    assert changed == [date(2026, 8, 6), date(2026, 8, 7)]
    journals = await TimelinePublicationJournal.find(
        TimelinePublicationJournal.user_id == str(user.id)
    ).to_list()
    assert len(journals) == 1
    assert [item.local_date for item in journals[0].affected_days] == changed
    # Nothing changed the second time: the hook reports real work, not days looked at.
    assert await refresh_projections(str(user.id), episode=episode) == []


def test_the_index_renderer_is_shared_with_the_settled_day_write():
    """chronicle.py must delegate, not keep a second copy that can drift."""

    # Imported here because loading the memory provider package at module import
    # would drag the full memory stack into every test in this file.
    from backend.services.memory.providers import chronicle

    assert chronicle._ensure_day_episode_index is projection.ensure_day_episode_index
    assert chronicle._render_day_episode_index is projection.render_day_episode_index
