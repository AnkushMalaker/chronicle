"""A cited recording must resolve to whatever is live now.

An episode names recordings by ``conversation_id``, but dedup, merge and trim all
replace the container while the audio stays put. Promotion only unhides live
recordings, so an unresolved reference means a meeting the agent identified is
silently never surfaced. Verified against real MongoDB:

    MONGODB_URI=mongodb://localhost:27017 uv run pytest tests/test_recording_refs.py
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.audio_capture import AudioRangeRef
from backend.models.audio_chunk import AudioChunkDocument
from backend.models.conversation import Conversation, create_conversation
from backend.models.timeline import EvidenceLocator, TimelineEvidenceRef
from backend.services.timeline.recording_refs import (
    build_audio_ranges,
    resolve_live_recordings,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("mongo_service"),
]

EPOCH = datetime(2026, 8, 7, 6, 30, tzinfo=timezone.utc)
STREAM = "u1-screenpipe-node-input"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def init_db():
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    name = os.getenv("TEST_DB_NAME", "test_recording_refs")
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


async def _recording(
    *,
    start=EPOCH,
    chunks=6,
    client_id=STREAM,
    deleted=False,
    reason=None,
    audio_range: AudioRangeRef | None = None,
):
    if audio_range is None:
        capture_id = f"capture-{uuid.uuid4()}"
        documents = []
        for index in range(chunks):
            document = AudioChunkDocument(
                user_id="u1",
                capture_source_id=client_id,
                capture_session_id=capture_id,
                sequence=index,
                audio_data=b"x",
                original_size=1,
                compressed_size=1,
                duration=10.0,
                captured_at=start + timedelta(seconds=index * 10.0),
            )
            await document.insert()
            documents.append(document)
        audio_range = AudioRangeRef(
            capture_source_id=client_id,
            time_basis="received",
            capture_session_ids=[capture_id],
            chunk_ids=[str(document.id) for document in documents],
            started_at=start,
            ended_at=start + timedelta(seconds=chunks * 10),
        )
    conversation = create_conversation(
        user_id="u1",
        client_id=client_id,
        audio_ranges=[audio_range],
        started_at=audio_range.started_at,
        ended_at=audio_range.ended_at,
    )
    conversation.audio_chunks_count = len(audio_range.chunk_ids)
    conversation.audio_total_duration = audio_range.duration_seconds
    conversation.deleted = deleted
    conversation.deletion_reason = reason
    await conversation.insert()
    return conversation


async def test_a_live_recording_resolves_to_itself(clean_db):
    live = await _recording()

    assert await resolve_live_recordings([live.conversation_id]) == {
        live.conversation_id
    }


async def test_a_merged_away_recording_resolves_through_its_lineage(clean_db):
    source = await _recording(deleted=True, reason="merged")
    merged = await _recording(start=EPOCH + timedelta(minutes=10))
    source.derived_into = [merged.conversation_id]
    await source.save()

    assert await resolve_live_recordings([source.conversation_id]) == {
        merged.conversation_id
    }


async def test_a_deduped_twin_resolves_by_the_audio_it_covered(clean_db):
    """Dedup records no lineage — the surviving copy never knew about this one."""

    twin = await _recording(deleted=True, reason="duplicate_screenpipe_ingest_retry")
    survivor = await _recording(audio_range=twin.audio_ranges[0])

    assert await resolve_live_recordings([twin.conversation_id]) == {
        survivor.conversation_id
    }


async def test_audio_from_another_stream_at_the_same_moment_is_not_the_same_recording(
    clean_db,
):
    """The other capture node, or the other direction, is different audio."""

    twin = await _recording(deleted=True, reason="duplicate_screenpipe_ingest_retry")
    await _recording(client_id="u1-screenpipe-node-output")

    assert await resolve_live_recordings([twin.conversation_id]) == set()


async def test_a_dead_recording_with_no_surviving_audio_resolves_to_nothing(clean_db):
    gone = await _recording(deleted=True, reason="merged")

    # Better to surface nothing than to promote unrelated audio: a wrong answer here
    # unhides ambient capture as though a person had been talking in it.
    assert await resolve_live_recordings([gone.conversation_id]) == set()


async def test_episode_audio_range_survives_chunk_rebinding(clean_db):
    recording = await _recording(chunks=3)
    evidence = TimelineEvidenceRef(
        evidence_id="transcript:source",
        kind="transcript",
        locator=EvidenceLocator(
            capture_source_id="recording-test",
            modality="transcript",
            track_id=None,
        ),
        started_at=EPOCH,
        ended_at=EPOCH + timedelta(seconds=30),
        role="user_statement",
        metadata={"conversation_id": recording.conversation_id},
    )

    ranges = await build_audio_ranges(
        started_at=EPOCH + timedelta(seconds=5),
        ended_at=EPOCH + timedelta(seconds=25),
        evidence_refs=[evidence],
    )

    assert len(ranges) == 1
    assert ranges[0].started_at == EPOCH + timedelta(seconds=5)
    assert ranges[0].ended_at == EPOCH + timedelta(seconds=25)
    original_ids = ranges[0].chunk_ids

    rebound = await _recording(audio_range=recording.audio_ranges[0])
    recording.deleted = True
    recording.derived_into = [rebound.conversation_id]
    await recording.save()

    persisted = await AudioChunkDocument.find({}).to_list()
    assert sorted(str(chunk.id) for chunk in persisted) == sorted(original_ids)
    assert rebound.audio_ranges[0].chunk_ids == recording.audio_ranges[0].chunk_ids
