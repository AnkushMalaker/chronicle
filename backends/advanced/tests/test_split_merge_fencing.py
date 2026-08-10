"""Split and merge must not launder what a recording is.

An operation that only moves audio must preserve the audio's identity, and
``create_conversation`` defaults every part of that identity off:

- ``data_purpose``/``memory_excluded`` — ScreenPipe audio is ingested as
  ``capture_evidence`` with ``memory_excluded=True``; a child that does not inherit
  them is memory-eligible, and an hour of ambient room audio walks into the vault.
- ``external_source_type``/``external_source_id`` — the timeline selects its audio
  evidence by source type and reads the capture direction out of the source id, so a
  child without them vanishes from the timeline and loses its media/speech role.
- ``created_at`` — a split child stamped with the moment the split ran is filed on the
  wrong day, however correct its audio is.

Verified against real MongoDB:

    MONGODB_URI=mongodb://localhost:27017 uv run pytest tests/test_split_merge_fencing.py
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.controllers import data_audit_controller as dac
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation, create_conversation

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("mongo_service"),
]

EPOCH = datetime(2026, 8, 6, 6, 11, 13, tzinfo=timezone.utc)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def init_db():
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    name = os.getenv("TEST_DB_NAME", "test_split_merge_fencing")
    await init_beanie(
        database=client[name], document_models=[AudioChunkDocument, Conversation]
    )
    yield
    await client.drop_database(name)
    client.close()


@pytest_asyncio.fixture(loop_scope="session")
async def clean_db(init_db):
    await AudioChunkDocument.delete_all()
    await Conversation.delete_all()
    yield


async def _evidence(
    n_chunks, start=EPOCH, *, excluded=True, purpose="capture_evidence"
):
    conv = create_conversation(
        user_id="u1",
        client_id="u1-screenpipe-abc-output",
        data_purpose=purpose,
        memory_excluded=excluded,
        memory_exclusion_reason="continuous_screenpipe_capture" if excluded else None,
        external_source_type="screenpipe",
        external_source_id="screenpipe:abc:output",
    )
    conv.created_at = start
    conv.audio_chunks_count = n_chunks
    conv.audio_total_duration = n_chunks * 10.0
    conv.add_transcript_version(
        version_id=f"v-{conv.conversation_id[:8]}",
        transcript="hello",
        words=[],
        segments=[
            Conversation.SpeakerSegment(speaker="a", start=1.0, end=5.0, text="hello")
        ],
        provider="test",
        set_as_active=True,
    )
    await conv.insert()
    for i in range(n_chunks):
        await AudioChunkDocument(
            conversation_id=conv.conversation_id,
            chunk_index=i,
            audio_data=b"x",
            original_size=1,
            compressed_size=1,
            start_time=i * 10.0,
            end_time=(i + 1) * 10.0,
            duration=10.0,
            captured_at=start + timedelta(seconds=i * 10.0),
        ).insert()
    return conv


async def test_merging_two_evidence_recordings_keeps_them_fenced(clean_db, monkeypatch):
    monkeypatch.setattr(dac, "start_post_conversation_jobs", lambda *a, **k: {})
    first = await _evidence(6)
    second = await _evidence(6, EPOCH + timedelta(seconds=60))
    user = type("U", (), {"user_id": "u1", "is_superuser": True, "id": "u1"})()

    await dac.merge_conversations(user, [first.conversation_id, second.conversation_id])

    merged = await Conversation.find_one(
        Conversation.deleted != True,  # noqa: E712
        {"derived_from.operation": "merge"},
    )
    assert merged is not None
    assert merged.memory_excluded is True
    assert merged.data_purpose == "capture_evidence"


async def test_merging_a_promoted_part_unfences_the_whole_span(clean_db, monkeypatch):
    """One promoted part means that stretch was judged conversational."""
    monkeypatch.setattr(dac, "start_post_conversation_jobs", lambda *a, **k: {})
    fenced = await _evidence(6)
    promoted = await _evidence(
        6, EPOCH + timedelta(seconds=60), excluded=False, purpose="conversation"
    )
    user = type("U", (), {"user_id": "u1", "is_superuser": True, "id": "u1"})()

    await dac.merge_conversations(
        user, [fenced.conversation_id, promoted.conversation_id]
    )

    merged = await Conversation.find_one(
        Conversation.deleted != True,  # noqa: E712
        {"derived_from.operation": "merge"},
    )
    assert merged.memory_excluded is False
    assert merged.data_purpose == "conversation"


async def test_splitting_evidence_keeps_every_child_fenced(clean_db, monkeypatch):
    monkeypatch.setattr(dac, "start_post_conversation_jobs", lambda *a, **k: {})
    monkeypatch.setattr(dac, "_delete_source_memories", _noop)
    conv = await _evidence(12)
    user = type("U", (), {"user_id": "u1", "is_superuser": True, "id": "u1"})()

    await dac.split_conversation(user, conv.conversation_id, [60.0])

    children = await Conversation.find(
        Conversation.deleted != True,  # noqa: E712
        {"derived_from.operation": "split"},
    ).to_list()
    assert len(children) == 2
    assert all(child.memory_excluded for child in children)
    assert all(child.data_purpose == "capture_evidence" for child in children)


async def test_children_keep_the_source_and_the_time_of_their_audio(
    clean_db, monkeypatch
):
    monkeypatch.setattr(dac, "start_post_conversation_jobs", lambda *a, **k: {})
    monkeypatch.setattr(dac, "_delete_source_memories", _noop)
    conv = await _evidence(12)
    user = type("U", (), {"user_id": "u1", "is_superuser": True, "id": "u1"})()

    await dac.split_conversation(user, conv.conversation_id, [60.0])

    children = sorted(
        await Conversation.find(
            Conversation.deleted != True,  # noqa: E712
            {"derived_from.operation": "split"},
        ).to_list(),
        key=lambda child: child.created_at,
    )
    assert [child.external_source_type for child in children] == ["screenpipe"] * 2
    assert [child.external_source_id for child in children] == [
        "screenpipe:abc:output"
    ] * 2
    # The second child begins 60s of audio into the parent, not when the split ran.
    assert _utc(children[0].created_at) == EPOCH
    assert _utc(children[1].created_at) == EPOCH + timedelta(seconds=60)


async def test_a_merged_recording_still_belongs_to_its_capture_source(
    clean_db, monkeypatch
):
    monkeypatch.setattr(dac, "start_post_conversation_jobs", lambda *a, **k: {})
    first = await _evidence(6)
    second = await _evidence(6, EPOCH + timedelta(seconds=60))
    user = type("U", (), {"user_id": "u1", "is_superuser": True, "id": "u1"})()

    await dac.merge_conversations(user, [first.conversation_id, second.conversation_id])

    merged = await Conversation.find_one(
        Conversation.deleted != True,  # noqa: E712
        {"derived_from.operation": "merge"},
    )
    assert merged.external_source_type == "screenpipe"
    assert merged.external_source_id == "screenpipe:abc:output"
    assert _utc(merged.created_at) == EPOCH


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _noop(*args, **kwargs):
    return None
