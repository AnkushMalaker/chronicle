import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from simple_speaker_recognition.api.routers import enrollment_audit
from simple_speaker_recognition.database import Base
from simple_speaker_recognition.database.models import Speaker, SpeakerAudioSegment, User


def test_delete_segment_removes_audio_from_enrollment_manifest(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    session = session_factory()
    session.add(User(id=1, username="owner"))
    session.add(Speaker(id="speaker-1", name="Speaker", user_id=1))
    segment = SpeakerAudioSegment(
        speaker_id="speaker-1",
        audio_file_path="1/speaker-1/clip.wav",
        start_time=0,
        end_time=1,
        duration_seconds=1,
        embedding=json.dumps([1.0, 0.0]),
    )
    session.add(segment)
    session.commit()
    segment_id = segment.id
    session.close()

    enrollment_dir = tmp_path / "enrollment_audio"
    speaker_dir = enrollment_dir / "1" / "speaker-1"
    speaker_dir.mkdir(parents=True)
    audio_path = speaker_dir / "clip.wav"
    audio_path.write_bytes(b"audio")
    manifest_path = speaker_dir / "enrollment_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "speaker_id": "speaker-1",
                "user_id": 1,
                "total_files": 1,
                "audio_files": [
                    {
                        "filename": "clip.wav",
                        "path": "1/speaker-1/clip.wav",
                    }
                ],
            }
        )
    )

    monkeypatch.setattr(enrollment_audit, "get_db_session", session_factory)
    monkeypatch.setattr(
        enrollment_audit,
        "get_auth",
        lambda: SimpleNamespace(
            enrollment_audio_dir=enrollment_dir,
            data_dir=tmp_path,
        ),
    )
    monkeypatch.setattr(
        enrollment_audit, "recompute_speaker_centroid", lambda *args: None
    )

    result = asyncio.run(
        enrollment_audit.delete_segment(segment_id, hard=False, db=SimpleNamespace())
    )

    assert result["deleted"] is True
    assert not audio_path.exists()
    assert (tmp_path / "quarantine" / "speaker-1" / f"{segment_id}_clip.wav").exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["audio_files"] == []
    assert manifest["total_files"] == 0
    with session_factory() as verification_session:
        assert verification_session.get(SpeakerAudioSegment, segment_id) is None
