import asyncio
import json

import numpy as np
from simple_speaker_recognition.core import unified_speaker_db
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import Base
from simple_speaker_recognition.database.models import Speaker, User
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_identification_survives_an_embeddingless_speaker(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        session.add(User(id=1, username="owner"))
        session.add_all(
            [
                Speaker(
                    id="speaker-before",
                    name="Before",
                    user_id=1,
                    embedding_data=json.dumps([1.0, 0.0]),
                ),
                Speaker(
                    id="speaker-without-embedding",
                    name="Not enrolled",
                    user_id=1,
                    embedding_data=None,
                ),
                Speaker(
                    id="speaker-after",
                    name="After",
                    user_id=1,
                    embedding_data=json.dumps([0.0, 1.0]),
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(unified_speaker_db, "get_db_session", session_factory)
    database = UnifiedSpeakerDB(emb_dim=2, base_dir=tmp_path, similarity_thr=0.5)

    found, speaker, confidence, candidates = asyncio.run(
        database.identify_with_candidates(np.array([0.0, 1.0]), user_id=1)
    )

    assert found is True
    assert speaker == {"id": "speaker-after", "name": "After", "user_id": 1}
    assert confidence == 1.0
    assert candidates[0]["id"] == "speaker-after"


def test_identification_uses_per_call_threshold_without_mutating_default(
    tmp_path, monkeypatch
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        session.add(User(id=1, username="owner"))
        session.add(
            Speaker(
                id="speaker",
                name="Speaker",
                user_id=1,
                embedding_data=json.dumps([1.0, 0.0]),
            )
        )
        session.commit()

    monkeypatch.setattr(unified_speaker_db, "get_db_session", session_factory)
    database = UnifiedSpeakerDB(emb_dim=2, base_dir=tmp_path, similarity_thr=0.5)
    # Scalar model inference returns one row, not a bare vector.
    query = np.array([[0.8, 0.6]], dtype=np.float32)

    async def identify_concurrently():
        return await asyncio.gather(
            database.identify_with_candidates(
                query, user_id=1, similarity_threshold=0.75
            ),
            database.identify_with_candidates(
                query, user_id=1, similarity_threshold=0.85
            ),
        )

    accepted, rejected = asyncio.run(identify_concurrently())

    assert accepted[0] is True
    assert rejected[0] is False
    assert accepted[2] == rejected[2] == 0.800000011920929
    assert database.similarity_thr == 0.5


def test_batch_identification_preserves_order_and_unknown_results(
    tmp_path, monkeypatch
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        session.add(User(id=1, username="owner"))
        session.add_all(
            [
                Speaker(
                    id="speaker-x",
                    name="Speaker X",
                    user_id=1,
                    embedding_data=json.dumps([1.0, 0.0]),
                ),
                Speaker(
                    id="speaker-y",
                    name="Speaker Y",
                    user_id=1,
                    embedding_data=json.dumps([0.0, 1.0]),
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(unified_speaker_db, "get_db_session", session_factory)
    database = UnifiedSpeakerDB(emb_dim=2, base_dir=tmp_path, similarity_thr=0.5)
    embeddings = np.asarray([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]], dtype=np.float32)

    results = asyncio.run(
        database.identify_batch_with_candidates(
            embeddings,
            user_id=1,
            similarity_threshold=0.8,
        )
    )

    assert [result[0] for result in results] == [True, True, False]
    assert [result[1]["id"] if result[1] else None for result in results] == [
        "speaker-x",
        "speaker-y",
        None,
    ]
    assert results[2][2] == np.float32(1 / np.sqrt(2))
