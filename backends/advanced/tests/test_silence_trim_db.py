"""The claim-only silence trim operation (real MongoDB).

Verifies that ``trim_silence`` clips a Conversation's range claims, re-times every
derived transcript version, and never moves, renumbers, or deletes capture chunks.

    MONGODB_URI=mongodb://localhost:27017 uv run pytest tests/test_silence_trim_db.py
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.audio_capture import (
    AudioRangeRef,
    ConversationTranscriptRevision,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation, create_conversation
from advanced_omi_backend.models.waveform import WaveformData
from advanced_omi_backend.workers.conversation_jobs import trim_silence

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("mongo_service"),
]

EPOCH = datetime(2026, 8, 7, 18, 17, 59, tzinfo=timezone.utc)


def _mongo_url():
    return os.getenv("MONGODB_URI", "mongodb://localhost:27018")


def _db_name():
    return os.getenv("TEST_DB_NAME", "test_silence_trim_db")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def init_db():
    client = AsyncIOMotorClient(_mongo_url())
    await init_beanie(
        database=client[_db_name()],
        # WaveformData too: a trim invalidates the caches derived from the audio it
        # just re-timed, and a stale waveform would describe a timeline that is gone.
        document_models=[
            AudioChunkDocument,
            Conversation,
            ConversationTranscriptRevision,
            WaveformData,
        ],
    )
    yield
    await client.drop_database(_db_name())
    client.close()


@pytest_asyncio.fixture(loop_scope="session")
async def clean_db(init_db):
    await AudioChunkDocument.delete_all()
    await Conversation.delete_all()
    await ConversationTranscriptRevision.delete_all()
    yield


async def _make_conversation_with_chunks(n_chunks, *, segments=None):
    """A visible conversation with ``n_chunks`` 10s chunks, anchored at EPOCH."""
    capture_id = f"capture-{uuid.uuid4()}"
    chunks = []
    for i in range(n_chunks):
        chunk = AudioChunkDocument(
            user_id="u1",
            capture_source_id="u1-phone",
            capture_session_id=capture_id,
            sequence=i,
            audio_data=b"x",
            original_size=1,
            compressed_size=1,
            duration=10.0,
            captured_at=EPOCH + timedelta(seconds=i * 10.0),
        )
        await chunk.insert()
        chunks.append(chunk)
    audio_range = AudioRangeRef(
        capture_source_id="u1-phone",
        time_basis="recorded",
        capture_session_ids=[capture_id],
        chunk_ids=[str(chunk.id) for chunk in chunks],
        started_at=EPOCH,
        ended_at=EPOCH + timedelta(seconds=n_chunks * 10),
    )
    conv = create_conversation(
        user_id="u1",
        client_id="u1-phone",
        title="Recording...",
        audio_ranges=[audio_range],
        started_at=audio_range.started_at,
        ended_at=audio_range.ended_at,
    )
    conv.audio_chunks_count = n_chunks
    conv.audio_total_duration = audio_range.duration_seconds
    if segments is not None:
        conv.add_transcript_version(
            version_id="v1",
            transcript=" ".join(s[2] for s in segments),
            words=[],
            segments=[
                Conversation.SpeakerSegment(
                    speaker="ankush", start=start, end=end, text=text
                )
                for start, end, text in segments
            ],
            provider="test",
            set_as_active=True,
        )
    await conv.insert()
    return conv


async def test_interior_silence_clips_only_the_semantic_claim(clean_db):
    """The shape continuous capture actually produces: speech in the middle only.

    This is the case the old leading-silence-only trim could not touch at all.
    """
    # 1800s (the ScreenPipe session cap): speech from 900s to 1000s, silence around it.
    conv = await _make_conversation_with_chunks(
        180, segments=[(905.0, 930.0, "hello"), (950.0, 995.0, "goodbye")]
    )

    plan = await trim_silence(conv.conversation_id, [(905.0, 995.0)])
    assert plan is not None

    # Speech at 905-995 padded by 5s each side is exactly 900-1000, i.e. chunks 90-99.
    refreshed = await Conversation.find_one(
        Conversation.conversation_id == conv.conversation_id
    )
    assert refreshed.audio_chunks_count == 10
    assert refreshed.audio_total_duration == 100.0

    claimed_ids = refreshed.audio_ranges[0].chunk_ids
    survivors = [
        chunk
        for chunk in await AudioChunkDocument.find({}).sort("+sequence").to_list()
        if str(chunk.id) in claimed_ids
    ]
    assert [c.sequence for c in survivors] == list(range(90, 100))

    # captured_at is untouched by the renumbering — the surviving audio still knows
    # when it happened, which is what the whole design rests on.
    assert survivors[0].captured_at.replace(tzinfo=timezone.utc) == EPOCH + timedelta(
        seconds=900
    )

    # The transcript moved with the audio: speech that was at 905s is now at 5s.
    version = next(v for v in refreshed.transcript_versions if v.version_id == "v1")
    assert [round(s.start, 1) for s in version.segments] == [5.0, 50.0]
    assert version.transcript == "hello goodbye"

    # The standalone read projection moved too; callers never observe a revision
    # whose timings refer to the pre-trim claim.
    revision = await ConversationTranscriptRevision.find_one(
        ConversationTranscriptRevision.revision_id
        == refreshed.active_transcript_revision_id
    )
    assert revision is not None
    assert [round(segment["start"], 1) for segment in revision.segments] == [5.0, 50.0]
    assert revision.metadata["audio_projection"]["operation"] == "silence_trim"

    # Silence is not re-parented into a synthetic semantic object.
    remnant = await Conversation.find_one(
        Conversation.deletion_reason == "silence_trim"
    )
    assert remnant is None

    # No audio lost: every original chunk still exists somewhere.
    assert await AudioChunkDocument.count() == 180


async def test_trimmed_audio_keeps_its_absolute_time(clean_db):
    """A remnant needs no "trimmed from here" note: its chunks carry the answer."""
    conv = await _make_conversation_with_chunks(180)

    plan = await trim_silence(conv.conversation_id, [(905.0, 995.0)])
    assert plan is not None

    chunks = await AudioChunkDocument.find({}).sort("+sequence").to_list()
    # Raw capture identity and wall-clock position are untouched.
    assert [c.sequence for c in chunks] == list(range(180))
    assert chunks[0].captured_at.replace(tzinfo=timezone.utc) == EPOCH
    assert chunks[-1].captured_at.replace(tzinfo=timezone.utc) == EPOCH + timedelta(
        seconds=1790
    )


async def test_every_transcript_version_is_re_timed_not_just_the_active_one(clean_db):
    """A version left on the old timeline is a delayed failure, not a dormant one.

    Trimming moves the audio all versions describe. One left behind keeps timings that
    outrun the audio, and it stays invisible until something activates it — a rebuild
    resetting to the ASR layer, a manual version switch — at which point speaker
    recognition fails on a segment that ends past the end of the recording.
    """
    conv = await _make_conversation_with_chunks(
        180, segments=[(905.0, 930.0, "hello"), (950.0, 995.0, "goodbye")]
    )
    conv.add_transcript_version(
        version_id="asr",
        transcript="hello goodbye",
        words=[],
        segments=[
            Conversation.SpeakerSegment(
                speaker="Unknown Speaker", start=start, end=end, text=text
            )
            for start, end, text in [(905.0, 930.0, "hello"), (950.0, 995.0, "goodbye")]
        ],
        provider="test",
        set_as_active=False,
    )
    await conv.save()

    assert await trim_silence(conv.conversation_id, [(905.0, 995.0)]) is not None

    refreshed = await Conversation.find_one(
        Conversation.conversation_id == conv.conversation_id
    )
    inactive = next(v for v in refreshed.transcript_versions if v.version_id == "asr")
    assert refreshed.active_transcript_version != "asr"
    assert [round(s.start, 1) for s in inactive.segments] == [5.0, 50.0]
    # Nothing describes audio that no longer exists.
    assert max(s.end for s in inactive.segments) <= refreshed.audio_total_duration


async def test_leading_silence_is_still_trimmed(clean_db):
    """Leading silence is now just the case where the only cut run is at the front."""
    # 1300s: 1200s of leading silence, then speech.
    conv = await _make_conversation_with_chunks(130)

    plan = await trim_silence(conv.conversation_id, [(1200.0, 1300.0)])
    assert plan is not None

    refreshed = await Conversation.find_one(
        Conversation.conversation_id == conv.conversation_id
    )
    # 1200s padded back to 1195 → snaps to the chunk starting at 1190.
    assert refreshed.audio_chunks_count == 11
    claimed_ids = refreshed.audio_ranges[0].chunk_ids
    survivors = [
        chunk
        for chunk in await AudioChunkDocument.find({}).sort("+sequence").to_list()
        if str(chunk.id) in claimed_ids
    ]
    assert survivors[0].sequence == 119
    assert survivors[0].captured_at.replace(tzinfo=timezone.utc) == EPOCH + timedelta(
        seconds=1190
    )
    assert await AudioChunkDocument.count() == 130


async def test_a_conversation_that_is_mostly_speech_is_left_alone(clean_db):
    conv = await _make_conversation_with_chunks(30)  # 300s

    plan = await trim_silence(conv.conversation_id, [(0.0, 300.0)])
    assert plan is None

    refreshed = await Conversation.find_one(
        Conversation.conversation_id == conv.conversation_id
    )
    assert refreshed.audio_chunks_count == 30
    assert await AudioChunkDocument.count() == 30
