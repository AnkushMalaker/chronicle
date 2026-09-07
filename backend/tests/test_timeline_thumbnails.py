import os
from datetime import date, datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.device_input import DeviceInputJob
from backend.models.timeline import TimelineDay, TimelineEpisode
from backend.services.timeline.thumbnails import (
    apply_frame_choice,
    process_episode_thumbnails,
)


def episode(**overrides):
    values = {
        "episode_id": "episode-1",
        "run_id": "run-1",
        "user_id": "user-1",
        "local_date": datetime(2026, 8, 6).date(),
        "timezone": "Asia/Kolkata",
        "started_at": datetime(2026, 8, 6, 17, 34, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 8, 6, 18, 29, tzinfo=timezone.utc),
        "kind": "gaming_session",
        "title": "Rematch multiplayer gaming",
        "summary": "Played Rematch.",
        "confidence": 0.8,
        "activity_mode": "foreground",
        "frame_shortlist": [
            {"frame_id": 38001, "data": b"menu", "content_type": "image/jpeg"},
            {"frame_id": 38921, "data": b"match", "content_type": "image/jpeg"},
        ],
        "thumbnail_state": "requested",
    }
    values.update(overrides)
    return TimelineEpisode.model_construct(**values)


def test_chosen_frame_becomes_the_episode_image_and_the_rest_are_dropped():
    """The picker keeps one frame; the shortlist exists only to be judged.

    Storing all six per episode would multiply the timeline's image storage for frames
    nothing reads again.
    """

    item = episode()

    assert apply_frame_choice(item, {"selected_frame_id": 38921}) is True
    assert item.representative_image == b"match"
    assert item.representative_image_type == "image/jpeg"
    assert item.thumbnail_state == "chosen"
    assert item.frame_shortlist == []


def test_an_episode_whose_frames_depict_nothing_is_marked_unavailable():
    """Null is a real answer — a locked or blank stretch has no representative frame.

    It must be terminal, otherwise the cron re-requests the same interval forever.
    """

    item = episode()

    assert apply_frame_choice(item, {"selected_frame_id": None}) is False
    assert item.representative_image is None
    assert item.thumbnail_state == "unavailable"
    assert item.frame_shortlist == []


def test_a_frame_that_was_not_offered_cannot_be_selected():
    """Guards the same id hallucination seen in observation curation."""

    item = episode()

    assert apply_frame_choice(item, {"selected_frame_id": 99999}) is False
    assert item.thumbnail_state == "unavailable"


@pytest.fixture
async def thumbnail_documents(mongo_service):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_timeline_thumbnails_db"]
    await init_beanie(
        database=database,
        document_models=[TimelineEpisode, TimelineDay, DeviceInputJob],
    )
    yield database
    await client.drop_database("test_timeline_thumbnails_db")
    client.close()


@pytest.mark.asyncio
async def test_an_episode_predating_the_field_is_still_given_a_thumbnail(
    thumbnail_documents,
):
    """Selection must mean "not terminal", not a list of the states we know about.

    Every episode written before `thumbnail_state` existed has no such field, and a
    Mongo `$in` skips a missing field while `$nin` matches it. Beanie supplies the
    "" default on read, so the model shows `thumbnail_state == ""` either way and
    hides the difference — the cron reported 0 requested against a day full of
    episodes that all looked eligible in Python.
    """

    start = datetime(2026, 8, 6, 15, 18, tzinfo=timezone.utc)
    await TimelineDay(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
    ).insert()
    # Inserted through the raw collection, because the model cannot express a
    # document that lacks one of its fields — which is exactly the stored shape.
    await thumbnail_documents["timeline_episodes"].insert_one(
        {
            "episode_id": "aoe4",
            "episode_key": "aoe4-key",
            "run_id": "run-one",
            "user_id": "user",
            "local_date": "2026-08-06",
            "timezone": "UTC",
            "started_at": start,
            "ended_at": start + timedelta(minutes=58),
            "kind": "gaming_session",
            "title": "Age of Empires IV session",
            "summary": "Played a match.",
            "confidence": 0.8,
            "activity_mode": "foreground",
            "source_ids": ["screenpipe-1"],
        }
    )

    counts = await process_episode_thumbnails()

    assert counts["requested"] == 1
    job = await DeviceInputJob.find_one({"purpose": "episode_frames"})
    assert job is not None
    assert job.payload["episode_id"] == "aoe4"
