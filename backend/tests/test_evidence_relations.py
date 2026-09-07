from datetime import date, datetime, timedelta, timezone

from backend.models.timeline import EvidenceLocator
from backend.services.timeline import evidence_relations
from backend.services.timeline.contracts import (
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)
from backend.services.timeline.evidence_relations import (
    ALGORITHM,
    infer_evidence_relations,
)

START = datetime(2026, 8, 19, 4, 30, tzinfo=timezone.utc)
MATCHING_TEXT = (
    "We should preserve immutable audio chunks and align every source clock "
    "before grouping the evidence into one semantic activity"
)


def _item(
    evidence_id: str,
    source_id: str | None,
    text: str,
    *,
    conversation_id: str,
    direction: str = "input",
    offset_minutes: int = 0,
    content_hash: str | None = None,
) -> TimelineEvidenceItem:
    started_at = START + timedelta(minutes=offset_minutes)
    return TimelineEvidenceItem(
        evidence_id=evidence_id,
        kind="transcript",
        source_id=source_id,
        locator=EvidenceLocator(
            capture_source_id=source_id or conversation_id,
            modality="transcript",
            track_id=direction,
        ),
        source_item_id=conversation_id,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=10),
        role="media_content" if direction == "output" else "uncertain",
        excerpt=text,
        content_hash=content_hash,
        metadata={
            "conversation_id": conversation_id,
            "direction": direction,
        },
    )


def _audio(
    evidence_id: str,
    source_id: str,
    conversation_id: str,
    *,
    direction: str = "input",
) -> TimelineEvidenceItem:
    return TimelineEvidenceItem(
        evidence_id=evidence_id,
        kind="audio_span",
        source_id=source_id,
        locator=EvidenceLocator(
            capture_source_id=source_id,
            modality="audio",
            track_id=direction,
        ),
        started_at=START,
        ended_at=START + timedelta(minutes=10),
        role="media_content" if direction == "output" else "uncertain",
        metadata={
            "conversation_id": conversation_id,
            "direction": direction,
        },
    )


def _manifest(*items: TimelineEvidenceItem) -> TimelineEvidenceManifest:
    return TimelineEvidenceManifest(
        user_id="user-one",
        local_date=date(2026, 8, 19),
        timezone="Asia/Kolkata",
        started_at=START,
        ended_at=START + timedelta(hours=1),
        evidence_revision="evidence-revision-one",
        windows=[],
        evidence=list(items),
    )


def test_different_sources_with_strong_ordered_overlap_corroborate():
    manifest = _manifest(
        _item("transcript:a", "wearable", MATCHING_TEXT, conversation_id="a"),
        _item(
            "transcript:b",
            "laptop",
            MATCHING_TEXT + " today",
            conversation_id="b",
        ),
    )

    first = infer_evidence_relations(manifest)
    replay = infer_evidence_relations(manifest)

    assert first.algorithm == ALGORITHM
    assert first.relation_count == 1
    assert first.relations[0].relation_type == "corroborates"
    assert first.relations[0].calibrated is False
    assert first.relations[0].relation_id == replay.relations[0].relation_id
    assert first.relations[0].signals["shared_tokens"] >= 10


def test_input_output_agreement_is_only_a_possible_echo():
    manifest = _manifest(
        _item(
            "transcript:mic",
            "mac-mic",
            MATCHING_TEXT,
            conversation_id="mic",
            direction="input",
        ),
        _item(
            "transcript:output",
            "mac-output",
            MATCHING_TEXT,
            conversation_id="output",
            direction="output",
        ),
    )

    preview = infer_evidence_relations(manifest)

    assert preview.relations[0].relation_type == "possible_echo"
    assert "opposite_capture_directions" in preview.relations[0].reason_codes
    assert any(
        "acoustic verification" in warning for warning in preview.relations[0].warnings
    )


def test_source_can_be_resolved_through_conversation_audio_span():
    manifest = _manifest(
        _audio("audio:a", "wearable", "conversation-a"),
        _audio("audio:b", "laptop", "conversation-b"),
        _item(
            "transcript:a",
            None,
            MATCHING_TEXT,
            conversation_id="conversation-a",
        ),
        _item(
            "transcript:b",
            None,
            MATCHING_TEXT,
            conversation_id="conversation-b",
        ),
    )

    preview = infer_evidence_relations(manifest)

    assert preview.resolved_transcript_count == 2
    assert preview.compared_transcript_count == 2
    assert preview.unresolved_source_count == 0
    assert preview.truncated is False
    assert preview.source_ids == ["laptop", "wearable"]
    assert (
        preview.relations[0].signals["left_source_resolution"]
        == "conversation_audio_span"
    )


def test_ambiguous_conversation_source_is_not_compared():
    manifest = _manifest(
        _audio("audio:a1", "wearable", "conversation-a"),
        _audio("audio:a2", "laptop", "conversation-a"),
        _item(
            "transcript:a",
            None,
            MATCHING_TEXT,
            conversation_id="conversation-a",
        ),
        _item(
            "transcript:b",
            "phone",
            MATCHING_TEXT,
            conversation_id="conversation-b",
        ),
    )

    preview = infer_evidence_relations(manifest)

    assert preview.unresolved_source_count == 1
    assert preview.resolved_transcript_count == 1
    assert preview.relations == []


def test_same_source_is_not_independent_corroboration():
    manifest = _manifest(
        _item("transcript:a", "wearable", MATCHING_TEXT, conversation_id="a"),
        _item("transcript:b", "wearable", MATCHING_TEXT, conversation_id="b"),
    )

    preview = infer_evidence_relations(manifest)

    assert preview.candidate_pair_count == 0
    assert preview.relations == []


def test_unrelated_simultaneous_transcripts_emit_no_conflict_or_relation():
    manifest = _manifest(
        _item("transcript:a", "wearable", MATCHING_TEXT, conversation_id="a"),
        _item(
            "transcript:b",
            "laptop",
            (
                "The football commentary moved to extra time while the crowd sang "
                "behind the broadcast announcer"
            ),
            conversation_id="b",
        ),
    )

    preview = infer_evidence_relations(manifest)

    assert preview.candidate_pair_count == 1
    assert preview.relations == []


def test_nonoverlapping_matching_text_is_not_a_candidate():
    manifest = _manifest(
        _item("transcript:a", "wearable", MATCHING_TEXT, conversation_id="a"),
        _item(
            "transcript:b",
            "laptop",
            MATCHING_TEXT,
            conversation_id="b",
            offset_minutes=20,
        ),
    )

    preview = infer_evidence_relations(manifest)

    assert preview.candidate_pair_count == 0
    assert preview.relations == []


def test_preview_reports_when_the_safety_cap_truncates_comparison(monkeypatch):
    monkeypatch.setattr(evidence_relations, "MAX_RESOLVED_TRANSCRIPTS", 1)
    manifest = _manifest(
        _item("transcript:a", "wearable", MATCHING_TEXT, conversation_id="a"),
        _item("transcript:b", "laptop", MATCHING_TEXT, conversation_id="b"),
    )

    preview = infer_evidence_relations(manifest)

    assert preview.resolved_transcript_count == 2
    assert preview.compared_transcript_count == 1
    assert preview.truncated is True
    assert any(
        "capped at 1 resolved transcripts" in warning for warning in preview.warnings
    )
