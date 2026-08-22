"""Candidate-pool attribution: both labelling paths must count.

The pipeline writes ``identified_as``; manual annotation apply writes only
``segment.speaker``. Hand-labelled clips must reach the guided-enrollment pool
and must not be dropped by the plausibility gate (their low similarity is the
reason to enroll them).
"""

from advanced_omi_backend.controllers.guided_enrollment_controller import (
    _effective_label,
    _information_score,
    _is_manual_label,
)


def test_pipeline_identification_wins_over_speaker_field():
    seg = {"identified_as": "alex", "speaker": "daksh"}
    assert _effective_label(seg) == "alex"
    assert not _is_manual_label(seg)


def test_manual_annotation_counts_as_attribution():
    seg = {"identified_as": None, "speaker": "daksh"}
    assert _effective_label(seg) == "daksh"
    assert _is_manual_label(seg)


def test_placeholder_labels_are_not_attribution():
    for name in ("Unknown Speaker 1", "Noise", "Background Speech", "", None):
        seg = {"identified_as": None, "speaker": name}
        assert _effective_label(seg) is None
        assert not _is_manual_label(seg)


def _clip(sim, manually_labeled):
    return {
        "scores": {
            "sim_centroid": sim,
            "max_clip_sim": 0.4,
            "best_other": {"score": 0.1},
        },
        "duration": 6.0,
        "current_label": "daksh",
        "manually_labeled": manually_labeled,
    }


def test_low_similarity_manual_clip_survives_plausibility_gate():
    assert _information_score(_clip(0.20, manually_labeled=False), 0.5) is None
    scored = _information_score(_clip(0.20, manually_labeled=True), 0.5)
    assert scored is not None
    assert any("manually annotated" in reason for reason in scored["reasons"])
