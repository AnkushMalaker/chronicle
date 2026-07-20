from advanced_omi_backend.controllers.guided_enrollment_controller import (
    _information_score,
)
from advanced_omi_backend.workers.speaker_discovery_jobs import (
    _active_segments,
    discover_speaker_candidates_job,
)


def test_discovery_job_is_rq_importable():
    assert discover_speaker_candidates_job.__module__ == (
        "advanced_omi_backend.workers.speaker_discovery_jobs"
    )
    assert discover_speaker_candidates_job.__name__ == "discover_speaker_candidates_job"


def test_active_segments_prefers_active_version():
    document = {
        "active_transcript_version": "active",
        "transcript_versions": [
            {"version_id": "old", "segments": [{"text": "old"}]},
            {"version_id": "active", "segments": [{"text": "current"}]},
        ],
    }

    assert _active_segments(document) == [{"text": "current"}]


def test_automatic_label_disagreement_remains_reviewable():
    candidate = {
        "conversation_id": "conversation-1",
        "start": 10.0,
        "duration": 8.0,
        "current_label": "anushpa",
        "speaker_name": "Janhavi",
        "scores": {
            "sim_centroid": 0.52,
            "max_clip_sim": 0.35,
            "best_other": {"name": "anushpa", "score": 0.49},
        },
    }

    scored = _information_score(candidate, threshold=0.5)

    assert scored is not None
    assert "currently labeled anushpa — possible mismatch" in scored["reasons"]


def test_clear_other_speaker_is_gated_out():
    candidate = {
        "duration": 8.0,
        "current_label": "anushpa",
        "speaker_name": "Janhavi",
        "scores": {
            "sim_centroid": 0.40,
            "max_clip_sim": 0.35,
            "best_other": {"name": "anushpa", "score": 0.55},
        },
    }

    assert _information_score(candidate, threshold=0.5) is None
