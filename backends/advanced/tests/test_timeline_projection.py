"""Incremental day projection: which days an episode touches, and what they render."""

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.timeline import TimelineDay, TimelineEpisode
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.memory import vault_manager
from advanced_omi_backend.services.timeline import projection
from advanced_omi_backend.services.timeline.projection import (
    affected_local_dates,
    refresh_day_projection,
    refresh_projections,
)
from advanced_omi_backend.services.timeline.timezone import canonical_timezone

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
async def projection_db(mongo_service, tmp_path, monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client[DB_NAME]
    await init_beanie(
        database=database, document_models=[TimelineEpisode, TimelineDay, User]
    )
    monkeypatch.setattr(vault_manager, "_DEFAULT_BASE_DIR", tmp_path)
    yield tmp_path
    await client.drop_database(DB_NAME)
    client.close()


async def _user(pipeline: str) -> User:
    user = User(
        email=f"{pipeline}-{os.urandom(4).hex()}@example.com",
        hashed_password="x",
        active_timeline_pipeline=pipeline,
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
        "pipeline": user.active_timeline_pipeline,
    }
    payload.update(overrides)
    episode = TimelineEpisode(**payload)
    await episode.insert()
    return episode


def _index(vault_root, user: User, local_date: date) -> str:
    note = vault_root / str(user.id) / "Daily" / f"{local_date.isoformat()}.md"
    return note.read_text(encoding="utf-8") if note.is_file() else ""


@pytest.mark.asyncio
async def test_a_cross_midnight_episode_is_projected_into_both_days(projection_db):
    user = await _user("rolling")
    await _episode(
        user,
        start=datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc),
        end=datetime(2026, 8, 7, 0, 30, tzinfo=timezone.utc),
        title="Late session",
    )

    for local_date in (date(2026, 8, 6), date(2026, 8, 7)):
        assert await refresh_day_projection(str(user.id), local_date, "UTC") is True
        assert "Late session" in _index(projection_db, user, local_date)


@pytest.mark.asyncio
async def test_replaying_an_unchanged_projection_reports_no_change(projection_db):
    user = await _user("rolling")
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
    )

    assert await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC") is True
    before = _index(projection_db, user, date(2026, 8, 6))
    assert await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC") is False
    assert _index(projection_db, user, date(2026, 8, 6)) == before


@pytest.mark.asyncio
async def test_a_superseded_revision_is_not_projected(projection_db):
    user = await _user("rolling")
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
    note = _index(projection_db, user, date(2026, 8, 6))
    assert "New bounds" in note
    assert "Old bounds" not in note


@pytest.mark.asyncio
async def test_rolling_rows_are_invisible_to_a_day_pipeline_user(projection_db):
    """The two writers must never render into the same projection."""

    user = await _user("day")
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
        title="Rolling work",
        pipeline="rolling",
    )
    await TimelineDay(
        user_id=str(user.id),
        local_date=date(2026, 8, 6),
        timezone=UTC,
        active_run_id="run-one",
    ).insert()

    assert await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC") is False
    assert "Rolling work" not in _index(projection_db, user, date(2026, 8, 6))


@pytest.mark.asyncio
async def test_day_pipeline_rows_are_invisible_to_a_rolling_user(projection_db):
    user = await _user("rolling")
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
        title="Day work",
        pipeline="day",
    )

    assert await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC") is False


@pytest.mark.asyncio
async def test_a_day_pipeline_projection_reads_only_the_published_generation(
    projection_db,
):
    user = await _user("day")
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
        title="Stale generation",
        run_id="run-old",
    )
    await _episode(
        user,
        start=datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
        end=datetime(2026, 8, 6, 11, tzinfo=timezone.utc),
        title="Published generation",
        run_id="run-new",
    )
    await TimelineDay(
        user_id=str(user.id),
        local_date=date(2026, 8, 6),
        timezone=UTC,
        active_run_id="run-new",
    ).insert()

    await refresh_day_projection(str(user.id), date(2026, 8, 6), "UTC")
    note = _index(projection_db, user, date(2026, 8, 6))
    assert "Published generation" in note
    assert "Stale generation" not in note


@pytest.mark.asyncio
async def test_a_rolling_user_gets_a_day_row_without_an_analysis_run(projection_db):
    user = await _user("rolling")
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
    # The day pipeline owns publication; the projection must not claim a generation.
    assert day.active_run_id is None


@pytest.mark.asyncio
async def test_the_publish_hook_refreshes_every_day_an_episode_touches(projection_db):
    user = await _user("rolling")
    episode = await _episode(
        user,
        start=datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc),
        end=datetime(2026, 8, 7, 0, 30, tzinfo=timezone.utc),
        title="Overnight",
    )

    changed = await refresh_projections(str(user.id), episode=episode)

    assert changed == [date(2026, 8, 6), date(2026, 8, 7)]
    # Nothing changed the second time: the hook reports real work, not days looked at.
    assert await refresh_projections(str(user.id), episode=episode) == []


def test_the_index_renderer_is_shared_with_the_settled_day_write():
    """chronicle.py must delegate, not keep a second copy that can drift."""

    # Imported here because loading the memory provider package at module import
    # would drag the full memory stack into every test in this file.
    from advanced_omi_backend.services.memory.providers import chronicle

    assert chronicle._ensure_day_episode_index is projection.ensure_day_episode_index
    assert chronicle._render_day_episode_index is projection.render_day_episode_index
