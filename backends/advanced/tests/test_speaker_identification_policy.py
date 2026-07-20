"""Conservative label-level speaker identification policy."""

import pytest

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
