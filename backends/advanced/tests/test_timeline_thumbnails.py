from datetime import datetime, timezone

from advanced_omi_backend.models.timeline import TimelineEpisode
from advanced_omi_backend.services.timeline.thumbnails import apply_frame_choice


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
