"""Conservative label-level speaker identification policy."""

from types import SimpleNamespace

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
    _apply_human_speaker_overlays,
    _propagate_cluster_identities,
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
