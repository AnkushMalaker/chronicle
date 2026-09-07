"""Speaker projection provenance must describe the path that actually ran."""

from backend.workers.speaker_jobs import _speaker_identification_mode


def test_pyannote_identification_is_recorded_as_cluster_centroid():
    assert (
        _speaker_identification_mode(
            ran_pyannote_diarization=True,
            used_word_timeline_fallback=False,
            use_per_segment=False,
        )
        == "cluster_centroid"
    )


def test_provider_identification_modes_remain_explicit():
    assert (
        _speaker_identification_mode(
            ran_pyannote_diarization=False,
            used_word_timeline_fallback=False,
            use_per_segment=True,
        )
        == "per_segment"
    )
    assert (
        _speaker_identification_mode(
            ran_pyannote_diarization=False,
            used_word_timeline_fallback=False,
            use_per_segment=False,
        )
        == "majority_vote"
    )


def test_word_timeline_fallback_records_no_identity_pass():
    assert (
        _speaker_identification_mode(
            ran_pyannote_diarization=True,
            used_word_timeline_fallback=True,
            use_per_segment=False,
        )
        == "none"
    )
