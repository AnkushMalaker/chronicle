import json

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from simple_speaker_recognition.core.enrollment_audit import (
    compute_audit,
    recompute_speaker_centroid,
)
from simple_speaker_recognition.database import Base
from simple_speaker_recognition.database.models import (
    EnrollmentAuditDecision,
    Speaker,
    SpeakerAudioSegment,
    User,
)


def test_confirmed_correct_clip_no_longer_counts_as_flagged():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id=1, username="owner"))
    session.add(Speaker(id="speaker-1", name="Vipul", user_id=1))
    session.flush()

    vectors = [np.array([1.0, 0.0]), np.array([-1.0, 0.0])]
    segments = []
    for index, vector in enumerate(vectors):
        segment = SpeakerAudioSegment(
            speaker_id="speaker-1",
            audio_file_path=f"1/speaker-1/{index}.wav",
            start_time=0,
            end_time=3,
            duration_seconds=3,
            embedding=json.dumps(vector.tolist()),
        )
        session.add(segment)
        segments.append(segment)
    session.commit()

    before = compute_audit(session, user_id=1)["speakers"][0]
    assert before["n_flagged"] == 2

    session.add(
        EnrollmentAuditDecision(
            segment_id=segments[0].id,
            decision="confirmed_correct",
        )
    )
    session.commit()

    after = compute_audit(session, user_id=1)["speakers"][0]
    reviewed = next(
        clip for clip in after["clips"] if clip["segment_id"] == segments[0].id
    )
    assert after["n_flagged"] == 1
    assert reviewed["flags"] == []
    assert reviewed["heuristic_flags"] == ["junk"]
    assert reviewed["review_state"] == "confirmed_correct"


def test_recompute_centroid_deduplicates_rows_for_same_audio_path():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id=1, username="owner"))
    speaker = Speaker(id="speaker-1", name="Vipul", user_id=1)
    session.add(speaker)
    session.flush()

    for path, vector, duration in (
        ("1/speaker-1/same.wav", [1.0, 0.0], 3.0),
        ("1/speaker-1/same.wav", [1.0, 0.0], 99.0),
        ("1/speaker-1/other.wav", [0.0, 1.0], 4.0),
    ):
        session.add(
            SpeakerAudioSegment(
                speaker_id=speaker.id,
                audio_file_path=path,
                start_time=0,
                end_time=duration,
                duration_seconds=duration,
                embedding=json.dumps(vector),
            )
        )
    session.commit()

    class IndexRecorder:
        rebuilds = 0
        saves = 0

        def _rebuild_faiss_mapping(self):
            self.rebuilds += 1

        def _save_faiss_index(self):
            self.saves += 1

    index = IndexRecorder()
    recompute_speaker_centroid(session, index, speaker.id)

    session.refresh(speaker)
    assert speaker.audio_sample_count == 2
    assert speaker.total_audio_duration == 7.0
    assert np.allclose(
        json.loads(speaker.embedding_data),
        np.array([1.0, 1.0]) / np.sqrt(2),
    )
    assert (index.rebuilds, index.saves) == (1, 1)
