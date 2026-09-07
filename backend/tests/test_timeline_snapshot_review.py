from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.models.timeline import (
    EpisodeRevisionRef,
    GroupRevisionRef,
    TimelineDay,
    TimelineDaySnapshot,
    TimelineEpisode,
    TimelineSemanticGroupRevision,
)
from backend.routers.modules import timeline_routes
from backend.services.timeline.consolidation import (
    _validated_suggestions,
    active_semantic_groups,
)
from backend.services.timeline.review import split_selection

BASE = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
SNAPSHOT_ID = "a" * 64


def _episode(index: int, start: int, end: int) -> TimelineEpisode:
    return TimelineEpisode.model_construct(
        episode_id=f"episode-{index}",
        episode_key=f"key-{index}",
        revision=3,
        run_id="analysis-only",
        user_id="user-one",
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
        started_at=BASE + timedelta(minutes=start),
        ended_at=BASE + timedelta(minutes=end),
        kind="work",
        title=f"Episode {index}",
        summary="Account",
        status="settled",
        confidence=0.9,
        activity_mode="foreground",
    )


def test_nonconsecutive_and_overlapping_members_keep_exact_revisions():
    episodes = [_episode(1, 0, 40), _episode(2, 20, 30), _episode(3, 50, 80)]
    raw = {
        "suggestions": [
            {
                "episode_labels": ["E01", "E03"],
                "title": "Resumed work",
                "reason": "The same task resumed around another activity",
                "confidence": 0.9,
            }
        ]
    }

    result = _validated_suggestions(raw, episodes, SNAPSHOT_ID)

    assert result[0].episode_ids == ["episode-1", "episode-3"]
    assert result[0].member_revisions == [
        EpisodeRevisionRef(episode_key="key-1", revision=3),
        EpisodeRevisionRef(episode_key="key-3", revision=3),
    ]
    assert result[0].source_snapshot_id == SNAPSHOT_ID


def test_active_groups_are_resolved_by_exact_snapshot_revision():
    member_refs = [
        EpisodeRevisionRef(episode_key="key-1", revision=3),
        EpisodeRevisionRef(episode_key="key-2", revision=3),
    ]
    active = TimelineSemanticGroupRevision(
        group_key="group-one",
        revision=1,
        member_revisions=member_refs,
        episode_ids=["episode-1", "episode-2"],
        source_snapshot_id=SNAPSHOT_ID,
        title="One activity",
        summary="One account",
        started_at=BASE,
        ended_at=BASE + timedelta(hours=1),
    )
    tombstone = active.model_copy(
        update={"revision": 2, "status": "tombstone", "source_snapshot_id": "b" * 64}
    )
    snapshot = TimelineDaySnapshot(
        snapshot_id=SNAPSHOT_ID,
        episode_revisions=member_refs,
        semantic_group_revisions=[
            GroupRevisionRef(
                owner_local_date=date(2026, 8, 6),
                group_key="group-one",
                revision=2,
            )
        ],
        evidence_state_hash="c" * 64,
    )
    day = TimelineDay.model_construct(
        user_id="user-one",
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
        current_snapshot=snapshot,
        current_snapshot_id=SNAPSHOT_ID,
        snapshot_state="ready",
        semantic_group_history=[active, tombstone],
    )

    assert active_semantic_groups(day) == []


def test_field_confirmation_does_not_change_episode_lifecycle():
    episode = _episode(1, 0, 30)

    timeline_routes._confirm(episode, ["title"])

    assert episode.status == "settled"
    assert episode.confirmed_fields == ["title"]
    assert episode.pinned is True


def test_cross_midnight_episode_has_one_semantic_memory_home_day():
    episode = _episode(1, 0, 30).model_copy(
        update={
            "started_at": datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc),
            "ended_at": datetime(2026, 8, 7, 0, 30, tzinfo=timezone.utc),
        }
    )
    first = TimelineDay.model_construct(
        user_id="user-one",
        local_date=date(2026, 8, 6),
        timezone="Etc/UTC",
    )
    second = TimelineDay.model_construct(
        user_id="user-one",
        local_date=date(2026, 8, 7),
        timezone="Etc/UTC",
    )

    batches = split_selection([episode], "Etc/UTC")
    assert batches == [[episode]]
    assert batches[0][0].started_at.date() == first.local_date


@pytest.mark.asyncio
async def test_manual_edit_publishes_a_new_exact_revision(monkeypatch):
    original = _episode(1, 0, 30)
    day = SimpleNamespace(current_snapshot_id=SNAPSHOT_ID)
    captured = {}

    async def publish(**kwargs):
        captured.update(kwargs)

    async def owned(_episode_id, _user):
        return original

    async def owner(_episode):
        return day

    monkeypatch.setattr(timeline_routes, "_owned_episode", owned)
    monkeypatch.setattr(timeline_routes, "day_for_exact_episode", owner)
    monkeypatch.setattr(timeline_routes, "publish_manual_episode_change", publish)

    payload = await timeline_routes.update_timeline_episode(
        original.episode_id,
        timeline_routes.EpisodeUpdate(title="Human title"),
        SimpleNamespace(id="user-one"),
    )

    successor = captured["successors"][0]
    assert original.title == "Episode 1"
    assert successor.episode_key == original.episode_key
    assert successor.revision == original.revision + 1
    assert successor.episode_id != original.episode_id
    assert successor.status == "settled"
    assert successor.confirmed_fields == ["title"]
    assert payload["title"] == "Human title"
