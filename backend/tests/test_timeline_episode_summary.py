from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from backend.models.timeline import (
    EvidenceLocator,
    TimelineEpisode,
    TimelineEvidenceRef,
)
from backend.services.timeline.episode_summary import (
    bounded_episode_transcript,
    episode_structure_is_stable,
    episode_summary_scope_hash,
)

START = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


def _episode() -> TimelineEpisode:
    return SimpleNamespace(
        episode_key="episode-key",
        revision=3,
        run_id="run",
        user_id="user",
        local_date=date(2026, 9, 2),
        timezone="Asia/Kolkata",
        started_at=START + timedelta(minutes=10),
        ended_at=START + timedelta(minutes=20),
        kind="meeting",
        title="Rishon huddle",
        summary="A short account.",
        conversational=True,
        confidence=0.9,
        activity_mode="foreground",
        evidence_refs=[],
        status="provisional",
        confirmed_fields=[],
    )


def _transcript_ref(
    identifier: str,
    text: str,
    *,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> TimelineEvidenceRef:
    return TimelineEvidenceRef(
        evidence_id=identifier,
        kind="transcript",
        source_id="screenpipe-source",
        source_item_id=identifier,
        locator=EvidenceLocator(
            capture_source_id="screenpipe-source",
            modality="transcript",
            track_id="input",
        ),
        started_at=started_at or START + timedelta(minutes=11),
        ended_at=ended_at or START + timedelta(minutes=12),
        role="user_statement",
        excerpt=text,
        content_hash=f"hash-{identifier}",
        metadata={"conversation_id": identifier},
    )


def test_episode_transcript_is_self_contained_in_published_evidence_refs():
    episode = _episode()
    episode.evidence_refs = [
        _transcript_ref("screenpipe-input", "Rishon: immutable evidence")
    ]

    projected = bounded_episode_transcript(episode)

    assert "SOURCE RECORDING screenpipe-input" in projected
    assert "Rishon: immutable evidence" in projected


def test_episode_transcript_clips_each_recording_to_semantic_bounds():
    episode = _episode()
    episode.evidence_refs = [
        _transcript_ref(
            "before",
            "A: before",
            started_at=START + timedelta(minutes=1),
            ended_at=START + timedelta(minutes=2),
        ),
        _transcript_ref("screenpipe-output", "Rishon: inside"),
        _transcript_ref(
            "after",
            "A: after",
            started_at=START + timedelta(minutes=21),
            ended_at=START + timedelta(minutes=22),
        ),
    ]

    projected = bounded_episode_transcript(episode)

    assert "Rishon: inside" in projected
    assert "before" not in projected
    assert "after" not in projected


def test_episode_transcript_preserves_overlapping_sources_for_model_reconciliation():
    episode = _episode()
    episode.evidence_refs = [
        _transcript_ref("screenpipe-input", "Speaker: same exchange"),
        _transcript_ref("screenpipe-output", "Speaker: same exchange"),
    ]

    projected = bounded_episode_transcript(episode)

    assert "SOURCE RECORDING screenpipe-input" in projected
    assert "SOURCE RECORDING screenpipe-output" in projected
    assert "Reconcile duplicates" in projected


def test_summary_scope_hash_ignores_day_state_but_changes_with_bounded_transcript():
    episode = _episode()
    episode.evidence_refs = [
        _transcript_ref("screenpipe-input", "Rishon: first version")
    ]

    first = episode_summary_scope_hash(episode)
    episode.local_date = date(2026, 9, 3)
    episode.run_id = "another-day-snapshot"
    assert episode_summary_scope_hash(episode) == first

    episode.evidence_refs[0].excerpt = "Rishon: corrected transcript"
    assert episode_summary_scope_hash(episode) != first


def test_summary_requires_settlement_or_exact_structural_confirmation():
    episode = _episode()
    assert episode_structure_is_stable(episode) is False

    episode.confirmed_fields = ["started_at", "ended_at", "evidence_refs"]
    assert episode_structure_is_stable(episode) is True

    episode.confirmed_fields = []
    episode.status = "settled"
    assert episode_structure_is_stable(episode) is True


def test_partial_structural_confirmation_does_not_stabilize_evidence_membership():
    episode = _episode()
    episode.confirmed_fields = ["started_at", "ended_at"]

    assert episode_structure_is_stable(episode) is False

    episode.confirmed_fields.append("evidence_refs")
    assert episode_structure_is_stable(episode) is True
