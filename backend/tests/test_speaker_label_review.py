from backend.controllers.data_audit_controller import (
    _select_speaker_review_batch,
    _speaker_review_candidates,
    _speaker_review_key,
)
from backend.models.conversation import Conversation
from backend.routers.modules.annotation_routes import _apply_diarization_label


def _document():
    return {
        "conversation_id": "conversation-1",
        "title": "Lunch",
        "created_at": "2026-08-09T12:00:00Z",
        "active_transcript_version": "active",
        "transcript_versions": [
            {
                "version_id": "active",
                "segments": [
                    {
                        "start": 0.0,
                        "end": 3.0,
                        "text": "first",
                        "speaker": "SPEAKER_00",
                        "identified_as": "Alex",
                        "confidence": 0.91,
                        "segment_type": "speech",
                    },
                    {
                        "start": 3.0,
                        "end": 5.0,
                        "text": "second",
                        "speaker": "SPEAKER_01",
                        "identified_as": "Kaushik",
                        "confidence": 0.88,
                        "segment_type": "speech",
                    },
                    {
                        "start": 5.0,
                        "end": 8.0,
                        "text": "third",
                        "speaker": "SPEAKER_00",
                        "identified_as": "Alex",
                        "confidence": 0.93,
                        "segment_type": "speech",
                    },
                ],
            }
        ],
    }


def test_review_queue_includes_every_identity_claim_without_anomaly_heuristics():
    candidates = _speaker_review_candidates(_document(), set())

    assert [candidate["claimed_speaker"] for candidate in candidates] == [
        "Alex",
        "Kaushik",
        "Alex",
    ]
    assert [item["position"] for item in candidates[1]["context"]] == [
        "before",
        "current",
        "after",
    ]


def test_reviewed_claim_is_removed_by_conversation_and_start_key():
    reviewed = {_speaker_review_key("conversation-1", 3.0)}

    candidates = _speaker_review_candidates(_document(), reviewed)

    assert [candidate["claimed_speaker"] for candidate in candidates] == [
        "Alex",
        "Alex",
    ]


def test_non_speech_and_unidentified_segments_are_not_identity_claims():
    doc = _document()
    segments = doc["transcript_versions"][0]["segments"]
    segments[0]["segment_type"] = "event"
    segments[1]["identified_as"] = None

    candidates = _speaker_review_candidates(doc, set())

    assert [candidate["claimed_speaker"] for candidate in candidates] == ["Alex"]


def test_human_relabel_clears_rejected_model_identity():
    segment = Conversation.SpeakerSegment(
        start=3.0,
        end=5.0,
        text="second",
        speaker="SPEAKER_01",
        identified_as="Kaushik",
        confidence=0.88,
    )

    _apply_diarization_label(segment, "Daksh")

    assert segment.speaker == "Daksh"
    assert segment.identified_as is None
    assert segment.confidence is None


def test_review_batch_never_repeats_a_conversation_and_diversifies_speakers():
    candidates = [
        {
            "review_key": f"c{i}:{i}",
            "conversation_id": f"c{i}",
            "claimed_speaker": "Alex" if i < 3 else f"Speaker {i}",
            "confidence": 0.5 + i * 0.03,
        }
        for i in range(8)
    ]

    batch = _select_speaker_review_batch(candidates, 5, 0.5, {}, {})

    assert len({item["conversation_id"] for item in batch}) == len(batch)
    assert len({item["claimed_speaker"] for item in batch}) >= 4
    assert {item["selection_lane"] for item in batch} == {"boundary", "control"}


def test_review_batch_prioritizes_conversations_not_reviewed_before():
    candidates = [
        {
            "review_key": f"{conversation}:0",
            "conversation_id": conversation,
            "claimed_speaker": "Alex",
            "confidence": confidence,
        }
        for conversation, confidence in (("already", 0.5), ("fresh", 0.7))
    ]

    batch = _select_speaker_review_batch(
        candidates,
        1,
        0.5,
        {"already": 10},
        {"Alex": 10},
    )

    assert batch[0]["conversation_id"] == "fresh"
