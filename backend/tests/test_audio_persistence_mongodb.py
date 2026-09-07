"""
Integration tests for MongoDB-based audio chunk persistence.

These tests require a running MongoDB instance and test the complete
audio chunk pipeline: encoding, storage, retrieval, and reconstruction.

Run with: pytest tests/test_audio_persistence_mongodb.py --mongodb-url=mongodb://localhost:27017
"""

import io
import os
import struct
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Binary
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

from backend.models.audio_capture import (
    AudioCaptureSession,
    AudioRangeRef,
    CaptureEffects,
)
from backend.models.audio_chunk import AudioChunkDocument
from backend.models.conversation import Conversation, create_conversation
from backend.services.audio_claims import apply_audio_ranges
from backend.utils.audio_chunk_utils import (
    build_wav_from_pcm,
    concatenate_chunks_to_pcm,
    convert_wav_to_chunks,
    decode_opus_to_pcm,
    encode_pcm_to_opus,
    reconstruct_wav_from_conversation,
    retrieve_audio_chunks,
    wait_for_audio_chunks,
)

# Test configuration


def get_mongodb_url():
    """Get MongoDB URL from environment or pytest args."""
    return os.getenv("MONGODB_URI", "mongodb://localhost:27018")


def get_test_db_name():
    """Get test database name."""
    return os.getenv("TEST_DB_NAME", "test_audio_chunks_db")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def mongodb_client(mongo_service):
    """Create MongoDB client for tests."""
    client = AsyncIOMotorClient(get_mongodb_url())
    yield client
    client.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def init_db(mongodb_client):
    """Initialize Beanie with test database."""
    db = mongodb_client[get_test_db_name()]

    await init_beanie(
        database=db,
        document_models=[AudioCaptureSession, AudioChunkDocument, Conversation],
    )

    yield db

    # Cleanup: Drop test database
    await mongodb_client.drop_database(get_test_db_name())


@pytest_asyncio.fixture(loop_scope="session")
async def clean_db(init_db):
    """Clean database before each test."""
    # Drop all collections
    await AudioChunkDocument.delete_all()
    await AudioCaptureSession.delete_all()
    await Conversation.delete_all()
    yield


# Test data generators


def generate_pcm_data(duration_seconds=1, sample_rate=16000):
    """Generate sample PCM audio data."""
    num_samples = int(sample_rate * duration_seconds)
    pcm_bytes = b""

    for i in range(num_samples):
        # Simple pattern (not actual audio, just valid PCM structure)
        value = int(32767 * (i % 100) / 100)
        pcm_bytes += struct.pack("<h", value)

    return pcm_bytes


def create_wav_file(pcm_data, output_path, sample_rate=16000):
    """Create a WAV file from PCM data."""
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_data)


async def store_claimed_chunks(
    conversation_id: str, count: int
) -> list[AudioChunkDocument]:
    """Persist capture-owned chunks and one semantic claim over them."""
    captured_at = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    capture_id = f"capture-{conversation_id}"
    await AudioCaptureSession(
        capture_session_id=capture_id,
        user_id="test-user",
        capture_source_id="test-client",
        client_id="test-client",
        origin="batch",
        time_basis="recorded",
        capture_epoch=0,
        processing_profile="imported",
        effects=CaptureEffects.not_applicable(),
        voice_session_id=None,
        status="complete",
        started_at=captured_at,
        ended_at=captured_at + timedelta(seconds=count * 10),
    ).insert()
    chunks = []
    for sequence in range(count):
        pcm_data = generate_pcm_data(duration_seconds=10)
        opus_data = await encode_pcm_to_opus(pcm_data)
        chunk = AudioChunkDocument(
            user_id="test-user",
            capture_source_id="test-client",
            capture_session_id=capture_id,
            sequence=sequence,
            audio_data=Binary(opus_data),
            original_size=len(pcm_data),
            compressed_size=len(opus_data),
            duration=10.0,
            captured_at=captured_at + timedelta(seconds=sequence * 10),
            sample_rate=16000,
            channels=1,
        )
        await chunk.insert()
        chunks.append(chunk)
    audio_range = AudioRangeRef(
        capture_source_id="test-client",
        time_basis="recorded",
        capture_session_ids=[capture_id],
        chunk_ids=[str(chunk.id) for chunk in chunks],
        started_at=captured_at,
        ended_at=captured_at + timedelta(seconds=count * 10),
    )
    conversation = create_conversation(
        conversation_id=conversation_id,
        user_id="test-user",
        client_id="test-client",
        audio_ranges=[audio_range],
        started_at=audio_range.started_at,
        ended_at=audio_range.ended_at,
    )
    await apply_audio_ranges(conversation, [audio_range], save=False)
    await conversation.insert()
    return chunks


# Integration Tests


@pytest.mark.asyncio(loop_scope="session")
class TestOpusCodecIntegration:
    """Test Opus encoding/decoding with real data."""

    async def test_encode_decode_roundtrip(self, clean_db):
        """Test complete encode-decode cycle preserves data structure."""
        # Generate 1 second of PCM
        pcm_data = generate_pcm_data(duration_seconds=1)

        # Encode to Opus
        opus_data = await encode_pcm_to_opus(pcm_data)

        # Verify compression
        assert len(opus_data) < len(pcm_data) * 0.2  # At least 80% compression

        # Decode back to PCM
        decoded_pcm = await decode_opus_to_pcm(opus_data)

        # Verify sizes match (allow small variance)
        assert abs(len(decoded_pcm) - len(pcm_data)) < 1000

    async def test_build_wav_from_pcm(self, clean_db):
        """Test WAV file construction."""
        pcm_data = generate_pcm_data(duration_seconds=1)

        wav_data = await build_wav_from_pcm(pcm_data)

        # Verify WAV structure
        assert wav_data[:4] == b"RIFF"
        assert b"WAVE" in wav_data

        # Verify readable by wave module
        wav_buffer = io.BytesIO(wav_data)
        with wave.open(wav_buffer, "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 16000
            frames = wav.readframes(wav.getnframes())
            assert len(frames) == len(pcm_data)


@pytest.mark.asyncio(loop_scope="session")
class TestMongoDBChunkStorage:
    """Test MongoDB chunk storage and retrieval."""

    async def test_store_and_retrieve_single_chunk(self, clean_db):
        """Test storing and retrieving a single audio chunk."""
        conversation_id = "test-conv-001"
        stored = await store_claimed_chunks(conversation_id, 1)

        # Retrieve chunk
        chunks = await retrieve_audio_chunks(conversation_id)

        assert len(chunks) == 1
        assert chunks[0].capture_session_id == f"capture-{conversation_id}"
        assert chunks[0].sequence == 0
        assert len(chunks[0].audio_data) == len(stored[0].audio_data)

    async def test_retrieve_multiple_chunks_in_order(self, clean_db):
        """Test retrieving multiple chunks in correct order."""
        conversation_id = "test-conv-002"
        num_chunks = 5
        await store_claimed_chunks(conversation_id, num_chunks)

        # Retrieve all chunks
        chunks = await retrieve_audio_chunks(conversation_id)

        assert len(chunks) == num_chunks
        # Verify semantic claim order, independent of query ordering.
        for i, chunk in enumerate(chunks):
            assert chunk.sequence == i

    async def test_retrieve_chunks_with_pagination(self, clean_db):
        """Test chunk retrieval with start_index and limit."""
        conversation_id = "test-conv-003"

        await store_claimed_chunks(conversation_id, 10)

        # Retrieve chunks 5-7 (3 chunks starting at index 5)
        chunks = await retrieve_audio_chunks(conversation_id, start_index=5, limit=3)

        assert len(chunks) == 3
        assert chunks[0].sequence == 5
        assert chunks[1].sequence == 6
        assert chunks[2].sequence == 7

    async def test_redis_source_id_is_an_idempotent_commit_key(self, clean_db):
        """A replay after insert-before-XACK cannot create duplicate audio."""
        common = {
            "user_id": "test-user",
            "capture_source_id": "test-client",
            "capture_session_id": "capture-idempotent",
            "audio_data": Binary(b"opus"),
            "original_size": 320,
            "compressed_size": 4,
            "duration": 0.01,
            "captured_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
            "source_stream": "audio:stream:test-client",
            "source_first_message_id": "1-0",
            "source_last_message_id": "1-0",
            "source_message_ids": ["1-0"],
        }
        await AudioChunkDocument(
            sequence=0,
            **common,
        ).insert()

        with pytest.raises(DuplicateKeyError):
            await AudioChunkDocument(
                sequence=1,
                **common,
            ).insert()


@pytest.mark.asyncio(loop_scope="session")
class TestWAVReconstruction:
    """Test complete WAV reconstruction from MongoDB chunks."""

    async def test_reconstruct_wav_from_single_chunk(self, clean_db):
        """Test reconstructing WAV from a single chunk."""
        conversation_id = "test-conv-004"
        await store_claimed_chunks(conversation_id, 1)

        # Reconstruct WAV
        wav_data = await reconstruct_wav_from_conversation(conversation_id)

        # Verify WAV
        assert wav_data[:4] == b"RIFF"
        wav_buffer = io.BytesIO(wav_data)
        with wave.open(wav_buffer, "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 16000

    async def test_reconstruct_wav_from_multiple_chunks(self, clean_db):
        """Test reconstructing WAV from multiple chunks."""
        conversation_id = "test-conv-005"
        num_chunks = 3

        await store_claimed_chunks(conversation_id, num_chunks)

        # Reconstruct complete WAV
        wav_data = await reconstruct_wav_from_conversation(conversation_id)

        # Verify WAV contains all chunks
        wav_buffer = io.BytesIO(wav_data)
        with wave.open(wav_buffer, "rb") as wav:
            frames = wav.readframes(wav.getnframes())
            # Should be approximately 30 seconds worth of data
            expected_size = 16000 * 2 * 30  # sample_rate * bytes_per_sample * seconds
            assert abs(len(frames) - expected_size) < 10000  # Allow some variance

    async def test_reconstruct_no_chunks_raises_error(self, clean_db):
        """Test reconstruction fails when no chunks exist."""
        with pytest.raises(ValueError, match="Conversation .* not found"):
            await reconstruct_wav_from_conversation("nonexistent-conv")


@pytest.mark.asyncio(loop_scope="session")
class TestWAVConversion:
    """Test WAV file to MongoDB chunk conversion."""

    async def test_convert_wav_to_chunks(self, clean_db, tmp_path):
        """Test converting WAV file to MongoDB chunks."""
        conversation_id = "test-conv-006"

        # Create test WAV file (1 second)
        pcm_data = generate_pcm_data(duration_seconds=1)
        wav_path = tmp_path / "test.wav"
        create_wav_file(pcm_data, wav_path)

        result = await convert_wav_to_chunks(
            user_id="test-user",
            capture_source_id="test-client",
            wav_file_path=wav_path,
            captured_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        )
        assert result.chunk_count == 1

        conversation = create_conversation(
            conversation_id=conversation_id,
            user_id="test-user",
            client_id="test-client",
            audio_ranges=[result.audio_range],
            started_at=result.audio_range.started_at,
            ended_at=result.audio_range.ended_at,
        )
        await apply_audio_ranges(conversation, [result.audio_range], save=False)
        await conversation.insert()

        # Verify chunks in MongoDB
        chunks = await retrieve_audio_chunks(conversation_id)
        assert len(chunks) == 1

        # Verify conversation metadata updated
        updated_conv = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        assert updated_conv.audio_chunks_count == 1
        assert updated_conv.audio_total_duration is not None
        assert updated_conv.audio_compression_ratio is not None

    async def test_convert_long_wav_creates_multiple_chunks(self, clean_db, tmp_path):
        """Test converting long WAV creates multiple chunks."""
        conversation_id = "test-conv-007"

        # Create 25-second WAV file
        pcm_data = generate_pcm_data(duration_seconds=25)
        wav_path = tmp_path / "long_test.wav"
        create_wav_file(pcm_data, wav_path)

        result = await convert_wav_to_chunks(
            user_id="test-user",
            capture_source_id="test-client",
            wav_file_path=wav_path,
            captured_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        )
        assert result.chunk_count == 3

        conversation = create_conversation(
            conversation_id=conversation_id,
            user_id="test-user",
            client_id="test-client",
            audio_ranges=[result.audio_range],
            started_at=result.audio_range.started_at,
            ended_at=result.audio_range.ended_at,
        )
        await apply_audio_ranges(conversation, [result.audio_range], save=False)
        await conversation.insert()

        # Verify all chunks stored
        chunks = await retrieve_audio_chunks(conversation_id)
        assert len(chunks) == 3

    async def test_finite_capture_retry_reuses_the_same_chunks(
        self, clean_db, tmp_path
    ):
        pcm_data = generate_pcm_data(duration_seconds=12)
        wav_path = tmp_path / "retry.wav"
        create_wav_file(pcm_data, wav_path)
        captured_at = datetime(2026, 8, 12, tzinfo=timezone.utc)

        first = await convert_wav_to_chunks(
            user_id="test-user",
            capture_source_id="screenpipe-input",
            wav_file_path=wav_path,
            captured_at=captured_at,
            capture_session_id="deterministic-capture",
            origin="screenpipe",
        )
        second = await convert_wav_to_chunks(
            user_id="test-user",
            capture_source_id="screenpipe-input",
            wav_file_path=wav_path,
            captured_at=captured_at,
            capture_session_id="deterministic-capture",
            origin="screenpipe",
        )

        assert second.audio_range.chunk_ids == first.audio_range.chunk_ids
        assert await AudioChunkDocument.count() == 2
        capture = await AudioCaptureSession.find_one(
            AudioCaptureSession.capture_session_id == "deterministic-capture"
        )
        assert capture.status == "complete"
        assert capture.content_sha256

    async def test_finite_duplicate_pcm_reuses_oldest_capture(self, clean_db, tmp_path):
        pcm_data = generate_pcm_data(duration_seconds=12)
        wav_path = tmp_path / "duplicate.wav"
        create_wav_file(pcm_data, wav_path)

        first = await convert_wav_to_chunks(
            user_id="test-user",
            capture_source_id="old-backup",
            wav_file_path=wav_path,
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            capture_session_id="oldest-surviving-capture",
            origin="import",
        )
        duplicate = await convert_wav_to_chunks(
            user_id="test-user",
            capture_source_id="new-backup",
            wav_file_path=wav_path,
            captured_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            capture_session_id="backup-copy",
            origin="import",
        )

        assert duplicate.capture_session_id == first.capture_session_id
        assert duplicate.audio_range.chunk_ids == first.audio_range.chunk_ids
        assert duplicate.audio_range.started_at == first.audio_range.started_at
        assert await AudioCaptureSession.count() == 1
        assert await AudioChunkDocument.count() == 2


@pytest.mark.asyncio(loop_scope="session")
class TestChunkWaiting:
    """Test waiting for MongoDB chunks to become available."""

    async def test_wait_for_chunks_immediate_success(self, clean_db):
        """Test wait succeeds when chunks already exist."""
        conversation_id = "test-conv-008"
        await store_claimed_chunks(conversation_id, 1)

        # Wait should succeed immediately
        result = await wait_for_audio_chunks(conversation_id, max_wait_seconds=5)
        assert result is True

    async def test_wait_for_chunks_timeout(self, clean_db):
        """Test wait times out when chunks don't exist."""
        result = await wait_for_audio_chunks("nonexistent-conv", max_wait_seconds=1)
        assert result is False


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
