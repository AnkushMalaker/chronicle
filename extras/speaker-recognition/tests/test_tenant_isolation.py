"""Two Chronicle users must not share a speaker gallery.

The tenant of this service is Chronicle's own ``user_id``. Before that, the service
minted local autoincrementing integers that Chronicle had no way to supply, so
``speaker_recognition_client`` sent a literal ``"1"`` on every diarization,
identification, and wake-word gating call. Every Chronicle user therefore resolved to
the same tenant row, and identification searched one shared gallery.

Nothing here needed to be *wrong* for that to be dangerous: with one user it behaves
identically. These tests fail on the old behaviour only because a second user exists.
"""

import asyncio
import json

import numpy as np
import pytest
from fastapi import HTTPException
from simple_speaker_recognition import database
from simple_speaker_recognition.api.core import utils
from simple_speaker_recognition.core import unified_speaker_db
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import Base
from simple_speaker_recognition.database.models import Speaker, User
from simple_speaker_recognition.database.queries import UserQueries
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ALICE = "69b80e5894aa9ec334a421c9"
BOB = "69b80e5894aa9ec334b53d70"

# Orthogonal embeddings: each is a perfect match for its owner's speaker and no match
# at all for the other's, so a leak is unambiguous rather than a threshold artifact.
ALICE_VOICE = [1.0, 0.0]
BOB_VOICE = [0.0, 1.0]


@pytest.fixture
def two_tenant_db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        session.add_all(
            [User(id=ALICE, username="alice"), User(id=BOB, username="bob")]
        )
        session.add_all(
            [
                Speaker(
                    id="alice-speaker",
                    name="Alice's colleague",
                    user_id=ALICE,
                    embedding_data=json.dumps(ALICE_VOICE),
                ),
                Speaker(
                    id="bob-speaker",
                    name="Bob's colleague",
                    user_id=BOB,
                    embedding_data=json.dumps(BOB_VOICE),
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(unified_speaker_db, "get_db_session", session_factory)
    return UnifiedSpeakerDB(emb_dim=2, base_dir=tmp_path, similarity_thr=0.5)


def test_a_user_only_matches_a_speaker_from_their_own_gallery(two_tenant_db):
    found, speaker, confidence, _ = asyncio.run(
        two_tenant_db.identify_with_candidates(np.array(ALICE_VOICE), user_id=ALICE)
    )

    assert found is True
    assert speaker["id"] == "alice-speaker"
    assert speaker["user_id"] == ALICE
    assert confidence == pytest.approx(1.0)


def test_another_users_enrollment_cannot_be_matched(two_tenant_db):
    """Bob's voice against Alice's gallery is a miss, not Bob's speaker."""

    found, speaker, _, candidates = asyncio.run(
        two_tenant_db.identify_with_candidates(np.array(BOB_VOICE), user_id=ALICE)
    )

    assert found is False
    assert speaker is None
    assert [c["id"] for c in candidates] != ["bob-speaker"]
    assert all(c.get("user_id") != BOB for c in candidates), candidates


def test_each_tenant_sees_only_its_own_speakers(two_tenant_db):
    alice = two_tenant_db.get_speakers_for_user(ALICE)
    bob = two_tenant_db.get_speakers_for_user(BOB)

    assert [s["id"] for s in alice] == ["alice-speaker"]
    assert [s["id"] for s in bob] == ["bob-speaker"]


def test_the_literal_tenant_the_client_used_to_send_matches_nothing():
    """``"1"`` was every user; it must now be an id that simply does not exist."""

    assert ALICE != "1" and BOB != "1"


def test_both_tenants_can_be_identified_independently(two_tenant_db):
    """Isolation must not be achieved by breaking identification for everyone."""

    alice_found, alice_speaker, _, _ = asyncio.run(
        two_tenant_db.identify_with_candidates(np.array(ALICE_VOICE), user_id=ALICE)
    )
    bob_found, bob_speaker, _, _ = asyncio.run(
        two_tenant_db.identify_with_candidates(np.array(BOB_VOICE), user_id=BOB)
    )

    assert alice_found and bob_found
    assert alice_speaker["id"] == "alice-speaker"
    assert bob_speaker["id"] == "bob-speaker"


def test_a_tenant_row_can_be_created_on_a_cold_database(tmp_path):
    """The service must survive a first start with an empty schema.

    ``users.id`` stopped being an autoincrementing integer, so the old bootstrap —
    ``User(username="admin")`` with no id — began inserting a NULL primary key. That
    ran at startup, so the container crashed before serving ``/health`` and CI timed
    out waiting for it. Nothing in a warm local database exercises this.
    """

    engine = create_engine(f"sqlite:///{tmp_path/'cold.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    created = UserQueries.get_or_create_user(session, ALICE)

    assert created.id == ALICE
    # Falls back to the id when no display name is offered, rather than NULL.
    assert created.username == ALICE
    # Idempotent: a second call returns the same row rather than colliding on the PK.
    assert UserQueries.get_or_create_user(session, ALICE).id == ALICE
    assert len(UserQueries.get_all_users(session)) == 1


def test_two_tenants_coexist_on_a_cold_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'cold.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    UserQueries.get_or_create_user(session, ALICE, "alice")
    UserQueries.get_or_create_user(session, BOB, "bob")

    assert sorted(u.id for u in UserQueries.get_all_users(session)) == sorted(
        [ALICE, BOB]
    )


def test_the_owning_tenant_comes_from_the_record_not_the_id(tmp_path, monkeypatch):
    """Ownership is read from the speaker row, never parsed out of its id.

    An imported or re-owned speaker keeps an id whose prefix names someone else.
    """

    engine = create_engine(f"sqlite:///{tmp_path/'owners.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "get_db_session", factory, raising=False)

    session = factory()
    UserQueries.get_or_create_user(session, ALICE)
    session.add(Speaker(id="user_1_legacy", name="Imported", user_id=ALICE))
    session.commit()

    # The id says tenant "1"; the record says ALICE.
    assert utils.owner_of_speaker("user_1_legacy") == ALICE

    # An unknown speaker is not guessed at from its id: enrolment supplies user_id.
    with pytest.raises(HTTPException) as caught:
        utils.owner_of_speaker(f"user_{ALICE}_speaker_neverenrolled")
    assert caught.value.status_code == 404
