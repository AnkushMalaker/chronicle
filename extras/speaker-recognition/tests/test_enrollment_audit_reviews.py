import json

import numpy as np
from simple_speaker_recognition.core.enrollment_audit import compute_audit
from simple_speaker_recognition.database import Base
from simple_speaker_recognition.database.models import (
    EnrollmentAuditDecision,
    Speaker,
    SpeakerAudioSegment,
    User,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


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
