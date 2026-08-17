"""Conservative label-level speaker identification policy."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.speaker_recognition_client import _select_label_mappings


def test_single_low_confidence_clip_does_not_name_speaker():
    mappings = _select_label_mappings(
        {"Speaker 0": [("Ankush", 0.55)]}, similarity_threshold=0.5
    )
    assert mappings == {}


def test_two_agreeing_clips_name_speaker():
    mappings = _select_label_mappings(
        {"Speaker 0": [("Ankush", 0.56), ("Ankush", 0.61)]},
        similarity_threshold=0.5,
    )
    assert mappings["Speaker 0"][0] == "Ankush"


def test_one_identity_cannot_be_assigned_to_two_labels():
    mappings = _select_label_mappings(
        {
            "Speaker 0": [("Ankush", 0.75), ("Ankush", 0.72)],
            "Speaker 1": [("Ankush", 0.61), ("Ankush", 0.60)],
        },
        similarity_threshold=0.5,
    )
    assert mappings == {"Speaker 0": ("Ankush", pytest.approx(0.735))}


# ---------------------------------------------------------------------------
# Cluster propagation: unidentified segments inherit their diarization
# cluster's agreeing confident IDs, except background-flagged segments.
# ---------------------------------------------------------------------------

from advanced_omi_backend.workers.speaker_jobs import (
    SpeakerDataIntegrityError,
    _apply_human_speaker_overlays,
    _compact_embedded_speaker_history,
    _compose_exclusive_projection,
    _merge_adjacent_projected_speech,
    _normalize_speaker_segment_bounds,
    _project_source_words_onto_speaker_turns,
    _propagate_cluster_identities,
    _rekey_cluster_centroids,
    _retained_non_speech_segments,
    _strip_reprojected_words_from_events,
    _word_timeline_fallback_segments,
)


def _seg(cluster, start, identified=None, confidence=None):
    return {
        "speaker": cluster,
        "start": start,
        "identified_as": identified,
        "confidence": confidence,
    }


def test_cluster_inherits_from_two_agreeing_votes():
    segments = [
        _seg("SPEAKER_00", 1.0, "ankush", 0.53),
        _seg("SPEAKER_00", 5.0, "ankush", 0.57),
        _seg("SPEAKER_00", 9.0),
    ]
    assert _propagate_cluster_identities(segments, set()) == 1
    assert segments[2]["identified_as"] == "ankush"
    assert segments[2]["confidence"] == pytest.approx(0.55)


def test_single_vote_does_not_propagate():
    segments = [_seg("SPEAKER_00", 1.0, "ankush", 0.6), _seg("SPEAKER_00", 5.0)]
    assert _propagate_cluster_identities(segments, set()) == 0
    assert segments[1]["identified_as"] is None


def test_conflicting_votes_do_not_propagate():
    segments = [
        _seg("SPEAKER_00", 1.0, "ankush", 0.6),
        _seg("SPEAKER_00", 5.0, "daksh", 0.55),
        _seg("SPEAKER_00", 9.0),
    ]
    assert _propagate_cluster_identities(segments, set()) == 0


def test_background_flagged_segments_do_not_inherit():
    segments = [
        _seg("SPEAKER_00", 1.0, "daksh", 0.55),
        _seg("SPEAKER_00", 5.0, "daksh", 0.59),
        _seg("SPEAKER_00", 217.6),  # TV audio diarization folded into the cluster
    ]
    assert _propagate_cluster_identities(segments, {217.6}) == 0
    assert segments[2]["identified_as"] is None


def test_noise_labels_neither_vote_nor_inherit():
    segments = [
        _seg("SPEAKER_00", 1.0, "Noise", 0.62),
        _seg("SPEAKER_00", 3.0, "Noise", 0.58),
        _seg("SPEAKER_00", 5.0),
    ]
    assert _propagate_cluster_identities(segments, set()) == 0
    assert segments[2]["identified_as"] is None


def test_cluster_centroid_prefers_person_identity_over_background_override():
    """A few Noise turns must not erase the person owning the neural cluster."""

    centroids = _rekey_cluster_centroids(
        {"SPEAKER_02": [1.0, 0.0]},
        [
            _seg("SPEAKER_02", 1.0, "roshan", 0.56),
            _seg("SPEAKER_02", 5.0, "roshan", 0.56),
            _seg("SPEAKER_02", 9.0, "Noise", 0.61),
        ],
        {},
    )

    assert centroids == {"roshan": [1.0, 0.0]}


def test_cluster_centroid_omits_cluster_with_conflicting_person_identities():
    centroids = _rekey_cluster_centroids(
        {"SPEAKER_00": [1.0, 0.0]},
        [
            _seg("SPEAKER_00", 1.0, "Alice", 0.7),
            _seg("SPEAKER_00", 5.0, "Bob", 0.7),
        ],
        {},
    )

    assert centroids == {}


def test_human_speaker_label_survives_reprocess_and_records_model_miss():
    segments = [
        Conversation.SpeakerSegment(
            start=99.625,
            end=105.005,
            text="annotated utterance",
            speaker="Unknown Speaker 3",
            identified_as=None,
            confidence=0.0,
        )
    ]
    annotations = [
        SimpleNamespace(
            id="annotation-1",
            segment_start_time=99.625,
            corrected_speaker="anshul tib",
        )
    ]

    failures = _apply_human_speaker_overlays(segments, annotations)

    assert segments[0].speaker == "anshul tib"
    assert segments[0].identified_as is None
    assert failures == [
        {
            "annotation_id": "annotation-1",
            "segment_start": 99.625,
            "human_speaker": "anshul tib",
            "model_speaker": None,
            "model_confidence": 0.0,
        }
    ]


def _projected(start, end, text, speaker="ankush", segment_type="speech"):
    return Conversation.SpeakerSegment(
        start=start,
        end=end,
        text=text,
        speaker=speaker if segment_type == "speech" else "",
        segment_type=segment_type,
        identified_as=speaker if segment_type == "speech" else None,
        confidence=0.8 if segment_type == "speech" else None,
        words=[],
    )


def test_same_speaker_projection_is_stitched_across_twenty_minute_seam():
    merged = _merge_adjacent_projected_speech(
        [
            _projected(1125.83, 1200.0, "before seam"),
            _projected(1200.031, 1254.318, "after seam"),
        ]
    )

    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (1125.83, 1254.318)
    assert merged[0].text == "before seam after seam"


def test_event_between_same_speaker_segments_remains_a_boundary():
    segments = [
        _projected(10.0, 11.0, "before"),
        _projected(11.0, 11.2, "[Noise]", segment_type="event"),
        _projected(11.2, 12.0, "after"),
    ]

    assert len(_merge_adjacent_projected_speech(segments)) == 3


def test_blank_zero_word_provider_events_are_not_reinserted_into_projection():
    meaningful = _projected(11.0, 11.2, "[Noise]", segment_type="event")
    blank = _projected(12.0, 12.2, "", segment_type="event")

    assert _retained_non_speech_segments([meaningful, blank]) == [meaningful]


def test_empty_pyannote_fallback_uses_word_clock_not_provider_boundaries():
    words = [
        {"word": "first", "start": 0.4, "end": 0.8, "confidence": 0.9},
        {"word": "sentence", "start": 0.8, "end": 1.2, "confidence": 0.8},
        {"word": "after", "start": 4.0, "end": 4.3, "confidence": 0.95},
        {"word": "silence", "start": 4.3, "end": 4.3, "confidence": 0.7},
    ]

    segments = _word_timeline_fallback_segments(
        words,
        duration=5.0,
        max_gap=2.0,
    )

    assert [
        (segment["start"], segment["end"], segment["text"], segment["speaker"])
        for segment in segments
    ] == [
        (0.4, 1.2, "first sentence", "WORD_TIMELINE_FALLBACK"),
        (4.0, 4.3, "after silence", "WORD_TIMELINE_FALLBACK"),
    ]
    assert [word for segment in segments for word in segment["words"]] == words


def test_empty_pyannote_fallback_rejects_word_outside_audio():
    with pytest.raises(SpeakerDataIntegrityError, match="outside 5.000s audio"):
        _word_timeline_fallback_segments(
            [{"word": "late", "start": 4.9, "end": 5.5}],
            duration=5.0,
        )


def test_speaker_model_frame_overhang_is_clipped_to_exact_audio_claim():
    segments = _normalize_speaker_segment_bounds(
        [
            {
                "start": 580.0,
                "end": 583.3,
                "duration": 3.3,
                "text": "last words",
                "speaker": "SPEAKER_00",
            }
        ],
        duration=583.298,
    )

    assert segments[0]["start"] == 580.0
    assert segments[0]["end"] == 583.298
    assert segments[0]["duration"] == pytest.approx(3.298)


def test_speaker_turn_well_outside_audio_claim_is_rejected():
    with pytest.raises(SpeakerDataIntegrityError, match="lies outside 10.000s audio"):
        _normalize_speaker_segment_bounds(
            [{"start": 9.0, "end": 10.2, "text": "late"}],
            duration=10.0,
        )


def test_neural_projection_assigns_every_source_word_once_and_ignores_provider_lists():
    words = [
        {"word": "I", "start": 0.1, "end": 0.2, "confidence": 0.9},
        {"word": "cross", "start": 0.9, "end": 1.1, "confidence": 0.8},
        {"word": "gap", "start": 1.4, "end": 1.5, "confidence": 0.7},
    ]
    projected = _project_source_words_onto_speaker_turns(
        [
            {
                "start": 0.0,
                "end": 1.0,
                "speaker": "SPEAKER_00",
                "text": "untrusted duplicate",
                "words": [words[0], words[0]],
            },
            {
                "start": 2.0,
                "end": 3.0,
                "speaker": "SPEAKER_01",
                "text": "untrusted omission",
                "words": [],
            },
        ],
        words,
    )

    assert [word for segment in projected for word in segment["words"]] == words
    assert [segment["text"] for segment in projected] == ["I cross gap", ""]


def test_neural_projection_rejects_words_when_no_turn_exists():
    with pytest.raises(SpeakerDataIntegrityError, match="without speaker turns"):
        _project_source_words_onto_speaker_turns(
            [],
            [{"word": "hello", "start": 0.0, "end": 0.5}],
        )


def test_event_overlay_does_not_duplicate_reprojected_source_word():
    event = Conversation.SpeakerSegment(
        start=1.0,
        end=1.2,
        text="[Laughter]",
        speaker="",
        segment_type="event",
        words=[Conversation.Word(word="laugh", start=1.0, end=1.2)],
    )

    stripped = _strip_reprojected_words_from_events(
        [event],
        [{"word": "laugh", "start": 1.0, "end": 1.2}],
    )

    assert stripped[0].text == "[Laughter]"
    assert stripped[0].words == []
    assert event.words[0].word == "laugh"


def test_point_event_splits_speech_without_losing_words_or_creating_overlap():
    speech = _projected(0.0, 10.0, "before after")
    speech.words = [
        Conversation.Word(word="before", start=1.0, end=2.0),
        Conversation.Word(word="after", start=8.0, end=9.0),
    ]
    marker = _projected(
        5.0,
        5.0,
        "[merged: 1 min gap between recordings elided]",
        segment_type="event",
    )

    projected = _compose_exclusive_projection([speech], [marker])

    assert [(segment.start, segment.end, segment.text) for segment in projected] == [
        (0.0, 5.0, "before"),
        (5.0, 5.0, "[merged: 1 min gap between recordings elided]"),
        (5.0, 10.0, "after"),
    ]


def test_positive_event_is_clipped_to_audio_not_already_claimed_by_speech():
    speech = _projected(0.0, 10.0, "spoken words")
    event = _projected(8.0, 12.0, "[Environmental Sounds]", segment_type="event")

    projected = _compose_exclusive_projection([speech], [event])

    assert [(segment.start, segment.end, segment.text) for segment in projected] == [
        (0.0, 10.0, "spoken words"),
        (10.0, 12.0, "[Environmental Sounds]"),
    ]


def test_archived_speaker_history_is_removed_from_embedded_projection(monkeypatch):
    created_at = datetime.now(timezone.utc)
    base = Conversation.TranscriptVersion(
        version_id="raw",
        transcript="hello",
        provider="smallest",
        metadata={},
        created_at=created_at,
    )
    old = Conversation.TranscriptVersion(
        version_id="old-speaker",
        transcript="hello",
        provider="smallest",
        metadata={
            "reprocessing_type": "speaker_diarization",
            "source_version_id": "raw",
        },
        created_at=created_at,
    )
    current = Conversation.TranscriptVersion(
        version_id="new-speaker",
        transcript="hello",
        provider="smallest",
        metadata={
            "reprocessing_type": "speaker_diarization",
            "source_version_id": "old-speaker",
        },
        created_at=created_at,
    )
    conversation = SimpleNamespace(
        conversation_id="conversation-1",
        transcript_versions=[base, old, current],
    )
    archived = AsyncMock(return_value=SimpleNamespace(revision_id="revision-1"))
    monkeypatch.setattr(
        "advanced_omi_backend.workers.speaker_jobs."
        "ConversationTranscriptRevision.find_one",
        archived,
    )

    removed = asyncio.run(
        _compact_embedded_speaker_history(
            conversation,
            keep_version_id=current.version_id,
        )
    )

    assert removed == 1
    assert [version.version_id for version in conversation.transcript_versions] == [
        "raw",
        "new-speaker",
    ]
    assert current.metadata["source_version_id"] == "raw"


def test_unarchived_speaker_history_blocks_compaction(monkeypatch):
    created_at = datetime.now(timezone.utc)
    old = Conversation.TranscriptVersion(
        version_id="old-speaker",
        transcript="hello",
        provider="smallest",
        metadata={
            "reprocessing_type": "speaker_diarization",
            "source_version_id": "raw",
        },
        created_at=created_at,
    )
    current = Conversation.TranscriptVersion(
        version_id="new-speaker",
        transcript="hello",
        provider="smallest",
        metadata={
            "reprocessing_type": "speaker_diarization",
            "source_version_id": "old-speaker",
        },
        created_at=created_at,
    )
    conversation = SimpleNamespace(
        conversation_id="conversation-1",
        transcript_versions=[
            Conversation.TranscriptVersion(
                version_id="raw",
                transcript="hello",
                provider="smallest",
                metadata={},
                created_at=created_at,
            ),
            old,
            current,
        ],
    )
    monkeypatch.setattr(
        "advanced_omi_backend.workers.speaker_jobs."
        "ConversationTranscriptRevision.find_one",
        AsyncMock(return_value=None),
    )

    with pytest.raises(SpeakerDataIntegrityError, match="standalone revision"):
        asyncio.run(
            _compact_embedded_speaker_history(
                conversation,
                keep_version_id=current.version_id,
            )
        )

    assert [version.version_id for version in conversation.transcript_versions] == [
        "raw",
        "old-speaker",
        "new-speaker",
    ]
