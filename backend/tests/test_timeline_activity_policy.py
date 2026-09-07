from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.services.device_audio_ingest import _coverage_profile
from backend.services.timeline.activity_policy import (
    episode_is_recording_only,
    recording_only_evidence,
    rejected_activity,
    rejection_basis,
)
from backend.services.timeline.context import compact_evidence
from backend.services.timeline.contracts import TimelineEvidenceItem
from backend.services.timeline.evidence import build_evidence_anchors

START = datetime(2026, 9, 4, 5, tzinfo=timezone.utc)


def test_paused_playback_with_continuous_files_is_covered_but_missing_file_time_is_not():
    # Neither silence nor VAD state participates in coverage arithmetic.
    items = [
        SimpleNamespace(captured_at=START, ended_at=START + timedelta(seconds=30)),
        SimpleNamespace(
            captured_at=START + timedelta(seconds=30),
            ended_at=START + timedelta(seconds=60),
        ),
    ]
    assert _coverage_profile(items, START, START + timedelta(seconds=60), 10)[:2] == (
        60,
        0,
    )
    items[1].captured_at += timedelta(seconds=3)
    assert _coverage_profile(items, START, START + timedelta(seconds=60), 10)[:2] == (
        57,
        3,
    )


def test_rejected_claim_is_scoped_to_unchanged_evidence_and_time():
    evidence = SimpleNamespace(
        evidence_id="audio:one",
        content_hash="original",
        kind="audio_span",
        metadata={"state": "no_speech"},
    )
    episode = SimpleNamespace(
        started_at=START,
        ended_at=START + timedelta(seconds=60),
        evidence_refs=[evidence],
        confirmed_fields=[],
    )
    decision = SimpleNamespace(
        action="episode_not_activity",
        after={"rejected_activity": rejection_basis(episode)},
    )
    assert rejected_activity(
        episode.started_at, episode.ended_at, [evidence], [decision]
    )
    evidence.content_hash = "new-content"
    assert not rejected_activity(
        episode.started_at, episode.ended_at, [evidence], [decision]
    )
    assert episode_is_recording_only(episode)
    episode.confirmed_fields = ["title"]
    assert not episode_is_recording_only(episode)


def test_compact_model_input_preserves_quiet_capture_facts():
    item = TimelineEvidenceItem(
        evidence_id="audio:one",
        kind="audio_span",
        source_id="laptop",
        locator={
            "capture_source_id": "laptop",
            "modality": "audio",
            "track_id": "output",
        },
        started_at=START,
        ended_at=START + timedelta(seconds=60),
        role="uncertain",
        metadata={
            "state": "no_speech",
            "covered_seconds": 57,
            "missing_seconds": 3,
            "acoustic_active_seconds": 0.2,
            "acoustic_quiet_seconds": 59.8,
        },
    )
    assert compact_evidence(item)["metadata"] == item.metadata


def test_speech_or_substantial_background_sound_is_not_auto_removed():
    item = SimpleNamespace(kind="audio_span", metadata={"state": "transcribed"})
    assert not recording_only_evidence([item])
    item.metadata = {"state": "unscored"}
    assert not recording_only_evidence([item])
    item.metadata = {"state": "no_speech", "acoustic_active_fraction": [0.8, 0.5]}
    assert not recording_only_evidence([item])
    item.metadata = {"state": "no_speech", "acoustic_active_fraction": [0, 0, 0]}
    assert recording_only_evidence([item])


def test_gap_count_does_not_invent_exact_gap_boundaries():
    gap = TimelineEvidenceItem(
        evidence_id="gap:one",
        kind="capture_gap",
        source_id="laptop",
        locator={
            "capture_source_id": "laptop",
            "modality": "audio",
            "track_id": "output",
        },
        started_at=START,
        ended_at=START + timedelta(seconds=60),
        role="uncertain",
        metadata={"within_span": True, "missing_seconds": 3},
    )
    assert build_evidence_anchors([gap]) == []
    assert gap.anchor_ids == []
