"""The leading-silence trim operation (real MongoDB).

Verifies that ``trim_leading_silence`` moves the pre-speech chunks onto a
soft-deleted remnant (audio kept in Mongo, just hidden), re-bases the surviving
chunks in place so the visible conversation starts at the first speech, and loses
no audio in the process. Run against a real Mongo:

    MONGODB_URI=mongodb://localhost:27017 uv run pytest tests/test_leading_silence_trim_db.py
"""

import os

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation, create_conversation
from advanced_omi_backend.workers.conversation_jobs import trim_leading_silence

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("mongo_service"),
]


def _mongo_url():
    return os.getenv("MONGODB_URI", "mongodb://localhost:27018")


def _db_name():
    return os.getenv("TEST_DB_NAME", "test_silence_trim_db")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def init_db():
    client = AsyncIOMotorClient(_mongo_url())
    await init_beanie(
        database=client[_db_name()],
        document_models=[AudioChunkDocument, Conversation],
    )
    yield
    await client.drop_database(_db_name())
    client.close()


@pytest_asyncio.fixture(loop_scope="session")
async def clean_db(init_db):
    await AudioChunkDocument.delete_all()
    await Conversation.delete_all()
    yield


async def _make_conversation_with_chunks(n_chunks):
    """A visible conversation with ``n_chunks`` 10s chunks (0..n*10s)."""
    conv = create_conversation(user_id="u1", client_id="u1-phone", title="Recording...")
    conv.audio_chunks_count = n_chunks
    conv.audio_total_duration = n_chunks * 10.0
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
        ).insert()
    return conv


async def test_leading_silence_is_moved_to_a_soft_deleted_remnant(clean_db):
    # 1300s total: 1200s of leading silence (120 chunks) then 100s of speech (10 chunks).
    conv = await _make_conversation_with_chunks(130)

    trimmed = await trim_leading_silence(
        conv.conversation_id, speech_start_time=1200.0, min_trim_seconds=30.0
    )
    assert trimmed is True

    # Visible conversation now begins at the speech: 10 chunks, re-indexed from 0,
    # times re-based so it starts at 0.
    refreshed = await Conversation.find_one(
        Conversation.conversation_id == conv.conversation_id
    )
    assert refreshed.deleted is False
    assert refreshed.audio_chunks_count == 10
    assert refreshed.audio_total_duration == 100.0

    survivors = (
        await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conv.conversation_id
        )
        .sort("+chunk_index")
        .to_list()
    )
    assert [c.chunk_index for c in survivors] == list(range(10))
    assert survivors[0].start_time == 0.0
    assert survivors[-1].end_time == 100.0

    # The leading silence lives on a soft-deleted remnant — hidden, but its audio is kept.
    remnant = await Conversation.find_one(
        Conversation.deletion_reason == "leading_silence"
    )
    assert remnant is not None
    assert remnant.deleted is True
    assert remnant.audio_chunks_count == 120
    remnant_chunks = await AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == remnant.conversation_id
    ).to_list()
    assert len(remnant_chunks) == 120

    # No audio lost: every original chunk still exists somewhere.
    assert await AudioChunkDocument.count() == 130


async def test_short_leading_silence_is_left_untouched(clean_db):
    conv = await _make_conversation_with_chunks(5)  # 50s total

    trimmed = await trim_leading_silence(
        conv.conversation_id, speech_start_time=8.0, min_trim_seconds=30.0
    )
    assert trimmed is False

    refreshed = await Conversation.find_one(
        Conversation.conversation_id == conv.conversation_id
    )
    assert refreshed.audio_chunks_count == 5
    assert await AudioChunkDocument.count() == 5
