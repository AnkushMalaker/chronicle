from types import SimpleNamespace

from simple_speaker_recognition.api.routers import enrollment
from simple_speaker_recognition.api.routers.enrollment import (
    audio_content_hash,
    existing_enrollment_hashes,
)


def test_audio_content_hash_matches_across_different_filenames():
    audio = b"RIFF same encoded audio content"

    assert audio_content_hash(audio) == audio_content_hash(audio)
    assert audio_content_hash(audio) != audio_content_hash(audio + b" changed")


def test_existing_hashes_detect_same_audio_under_a_different_filename(
    tmp_path, monkeypatch
):
    enrollment_dir = tmp_path / "enrollment_audio"
    speaker_dir = enrollment_dir / "1" / "ankush"
    speaker_dir.mkdir(parents=True)
    audio = b"RIFF duplicate clip"
    (speaker_dir / "005150.wav").write_bytes(audio)
    monkeypatch.setattr(
        enrollment,
        "get_auth",
        lambda: SimpleNamespace(enrollment_audio_dir=enrollment_dir),
    )

    hashes = existing_enrollment_hashes(1, "ankush")

    assert audio_content_hash(audio) in hashes
