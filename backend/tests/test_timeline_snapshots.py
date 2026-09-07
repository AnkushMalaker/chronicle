"""Canonical Timeline snapshot identity and evidence provenance."""

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.models.timeline import (
    EpisodeRevisionRef,
    EvidenceLocator,
    GroupRevisionRef,
    ResolvedBoundarySupport,
    TimelineDay,
    TimelineEpisode,
    TimelineEvidenceRef,
    TimelineSemanticGroupRevision,
)
from backend.services.timeline.snapshots import (
    build_day_snapshot,
    evidence_state_hash_for_episodes,
    snapshot_from_projection,
    verify_day_snapshot,
)


def _episode(**overrides) -> TimelineEpisode:
    payload = {
        "run_id": "rolling:range-1",
        "user_id": "user-1",
        "local_date": date(2026, 9, 3),
        "timezone": "UTC",
        "started_at": datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 9, 3, 9, tzinfo=timezone.utc),
        "kind": "work",
        "title": "Build snapshots",
        "summary": "",
        "confidence": 0.9,
        "activity_mode": "foreground",
        "pipeline": "rolling",
        "episode_key": "episode-a",
        "revision": 2,
        "evidence_revision": 7,
    }
    payload.update(overrides)
    # Snapshot hashing is pure; avoid requiring Beanie collection initialization for
    # an otherwise ordinary model instance.
    return TimelineEpisode.model_construct(**payload)


def test_snapshot_hash_is_canonical_and_excludes_created_at():
    episode_refs = [
        EpisodeRevisionRef(episode_key="z", revision=3),
        EpisodeRevisionRef(episode_key="a", revision=1),
    ]
    group_refs = [
        GroupRevisionRef(owner_local_date=date(2026, 9, 3), group_key="g", revision=2)
    ]
    first = build_day_snapshot(
        user_id="user-1",
        local_date=date(2026, 9, 3),
        timezone_name="UTC",
        evidence_state_hash="e" * 64,
        episode_revisions=episode_refs,
        semantic_group_revisions=group_refs,
        created_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )
    second = build_day_snapshot(
        user_id="user-1",
        local_date=date(2026, 9, 3),
        timezone_name="UTC",
        evidence_state_hash="e" * 64,
        episode_revisions=reversed(episode_refs),
        semantic_group_revisions=group_refs,
        created_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )

    assert first.snapshot_id == second.snapshot_id
    assert [item.episode_key for item in first.episode_revisions] == ["a", "z"]
    verify_day_snapshot(
        first,
        user_id="user-1",
        local_date=date(2026, 9, 3),
        timezone_name="UTC",
    )


def test_snapshot_hash_includes_day_user_and_exact_member_revision():
    def snapshot(*, user="user-1", day=date(2026, 9, 3), revision=1):
        return build_day_snapshot(
            user_id=user,
            local_date=day,
            timezone_name="UTC",
            evidence_state_hash="e" * 64,
            episode_revisions=[
                EpisodeRevisionRef(episode_key="episode-a", revision=revision)
            ],
        )

    baseline = snapshot()
    assert snapshot(user="user-2").snapshot_id != baseline.snapshot_id
    assert snapshot(day=date(2026, 9, 4)).snapshot_id != baseline.snapshot_id
    assert snapshot(revision=2).snapshot_id != baseline.snapshot_id


def test_timeline_day_rejects_removed_semantic_groups_state():
    assert "semantic_groups" not in TimelineDay.model_fields

    with pytest.raises(ValueError, match="TimelineDay.semantic_groups was removed"):
        TimelineDay.model_validate(
            {
                "user_id": "user-1",
                "local_date": date(2026, 9, 3),
                "timezone": "UTC",
                "semantic_groups": [],
            }
        )


def test_snapshot_projection_accepts_only_exact_group_revisions():
    first = _episode(episode_id="episode-a", episode_key="episode-a", revision=2)
    second = _episode(
        episode_id="episode-b",
        episode_key="episode-b",
        revision=4,
        started_at=datetime(2026, 9, 3, 10, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 3, 11, tzinfo=timezone.utc),
    )
    group = TimelineSemanticGroupRevision(
        group_key="group-a",
        revision=3,
        member_revisions=[
            EpisodeRevisionRef(episode_key=first.episode_key, revision=first.revision),
            EpisodeRevisionRef(
                episode_key=second.episode_key, revision=second.revision
            ),
        ],
        episode_ids=[first.episode_id, second.episode_id],
        source_snapshot_id="s" * 64,
        title="One activity",
        summary="Two exact episode revisions.",
        started_at=first.started_at,
        ended_at=second.ended_at,
    )

    snapshot = snapshot_from_projection(
        user_id="user-1",
        local_date=date(2026, 9, 3),
        timezone_name="UTC",
        episodes=[first, second],
        semantic_group_revisions=[group],
    )

    assert snapshot.semantic_group_revisions == [
        GroupRevisionRef(
            owner_local_date=date(2026, 9, 3),
            group_key="group-a",
            revision=3,
        )
    ]
    with pytest.raises(TypeError, match="semantic_groups"):
        snapshot_from_projection(
            user_id="user-1",
            local_date=date(2026, 9, 3),
            timezone_name="UTC",
            episodes=[first, second],
            semantic_groups=[],
        )


def test_snapshot_rejects_two_active_revisions_of_one_key():
    with pytest.raises(ValueError, match="multiple active revisions"):
        build_day_snapshot(
            user_id="user-1",
            local_date=date(2026, 9, 3),
            timezone_name="UTC",
            evidence_state_hash="e" * 64,
            episode_revisions=[
                EpisodeRevisionRef(episode_key="episode-a", revision=1),
                EpisodeRevisionRef(episode_key="episode-a", revision=2),
            ],
        )


def test_resolved_boundary_provenance_changes_the_evidence_and_snapshot_hash():
    instant = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
    locator = EvidenceLocator(
        capture_source_id="screenpipe-host", modality="screen", track_id="display-1"
    )
    support = ResolvedBoundarySupport(
        anchor_id="anchor-1",
        evidence_id="evidence-1",
        locator=locator,
        support_type="frame",
        earliest_at=instant,
        latest_at=instant + timedelta(seconds=2),
        resolved_at=instant + timedelta(seconds=1),
        resolved_source_position="frame-20",
        uncertainty_seconds=1,
        separation_artifact_hash="artifact-1",
    )
    evidence = TimelineEvidenceRef(
        evidence_id="evidence-1",
        kind="frame",
        started_at=instant,
        role="application_state",
        locator=locator,
        start_boundary_support=[support],
    )
    before = _episode(evidence_refs=[evidence])
    after_support = support.model_copy(update={"resolved_source_position": "frame-21"})
    after = _episode(
        evidence_refs=[
            evidence.model_copy(update={"start_boundary_support": [after_support]})
        ]
    )

    assert evidence_state_hash_for_episodes(
        [before]
    ) != evidence_state_hash_for_episodes([after])
    assert (
        snapshot_from_projection(
            user_id="user-1",
            local_date=date(2026, 9, 3),
            timezone_name="UTC",
            episodes=[before],
        ).snapshot_id
        != snapshot_from_projection(
            user_id="user-1",
            local_date=date(2026, 9, 3),
            timezone_name="UTC",
            episodes=[after],
        ).snapshot_id
    )


def test_evidence_hash_is_stable_across_mongo_datetime_round_trip():
    aware = datetime(
        2026, 9, 3, 8, 0, 0, 123456, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    mongo_utc = aware.astimezone(timezone.utc).replace(microsecond=123000, tzinfo=None)
    locator = EvidenceLocator(
        capture_source_id="screenpipe-host", modality="screen", track_id="display-1"
    )
    aware_support = ResolvedBoundarySupport(
        anchor_id="anchor-1",
        evidence_id="evidence-1",
        locator=locator,
        support_type="frame",
        earliest_at=aware,
        latest_at=aware + timedelta(seconds=2),
        resolved_at=aware + timedelta(seconds=1),
        separation_artifact_hash="artifact-1",
    )
    mongo_support = aware_support.model_copy(
        update={
            "earliest_at": mongo_utc,
            "latest_at": mongo_utc + timedelta(seconds=2),
            "resolved_at": mongo_utc + timedelta(seconds=1),
        }
    )
    aware_ref = TimelineEvidenceRef(
        evidence_id="evidence-1",
        kind="frame",
        started_at=aware,
        ended_at=aware + timedelta(seconds=2),
        role="application_state",
        locator=locator,
        start_boundary_support=[aware_support],
    )
    mongo_ref = aware_ref.model_copy(
        update={
            "started_at": mongo_utc,
            "ended_at": mongo_utc + timedelta(seconds=2),
            "start_boundary_support": [mongo_support],
        }
    )

    assert evidence_state_hash_for_episodes(
        [_episode(evidence_refs=[aware_ref])]
    ) == evidence_state_hash_for_episodes([_episode(evidence_refs=[mongo_ref])])


def test_resolved_boundary_must_fall_inside_its_support_window():
    instant = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="inside the boundary support window"):
        ResolvedBoundarySupport(
            anchor_id="anchor-1",
            evidence_id="evidence-1",
            locator=EvidenceLocator(
                capture_source_id="mic", modality="audio", track_id="input"
            ),
            support_type="sample",
            earliest_at=instant,
            latest_at=instant + timedelta(seconds=1),
            resolved_at=instant + timedelta(seconds=2),
            separation_artifact_hash="artifact-1",
        )


def test_semantic_revision_changes_snapshot_without_relabeling_evidence_state():
    instant = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
    evidence = TimelineEvidenceRef(
        evidence_id="evidence-1",
        kind="observation",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-test",
            modality="screen",
            track_id="display-1",
        ),
        started_at=instant,
        role="user_action",
        content_hash="source-content",
    )
    first = _episode(revision=1, evidence_refs=[evidence])
    second = _episode(revision=2, title="Edited title", evidence_refs=[evidence])

    assert evidence_state_hash_for_episodes(
        [first]
    ) == evidence_state_hash_for_episodes([second])
    assert (
        snapshot_from_projection(
            user_id="user-1",
            local_date=date(2026, 9, 3),
            timezone_name="UTC",
            episodes=[first],
        ).snapshot_id
        != snapshot_from_projection(
            user_id="user-1",
            local_date=date(2026, 9, 3),
            timezone_name="UTC",
            episodes=[second],
        ).snapshot_id
    )
