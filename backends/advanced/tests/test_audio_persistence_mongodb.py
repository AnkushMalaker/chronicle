"""
Integration tests for MongoDB-based audio chunk persistence.

These tests require a running MongoDB instance and test the complete
audio chunk pipeline: encoding, storage, retrieval, and reconstruction.

Run with: pytest tests/test_audio_persistence_mongodb.py --mongodb-url=mongodb://localhost:27017
"""

import asyncio
import io
import os
import struct
import wave
from pathlib import Path

import pytest
from beanie import init_beanie
from bson import Binary
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.utils.audio_chunk_utils import (
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


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def mongodb_client():
    """Create MongoDB client for tests."""
    client = AsyncIOMotorClient(get_mongodb_url())
    yield client
    client.close()


@pytest.fixture(scope="session")
async def init_db(mongodb_client):
    """Initialize Beanie with test database."""
    db = mongodb_client[get_test_db_name()]

    await init_beanie(database=db, document_models=[AudioChunkDocument, Conversation])

    yield db

    # Cleanup: Drop test database
    await mongodb_client.drop_database(get_test_db_name())


@pytest.fixture
async def clean_db(init_db):
    """Clean database before each test."""
    # Drop all collections
    await AudioChunkDocument.delete_all()
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


# Integration Tests


@pytest.mark.asyncio
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


@pytest.mark.asyncio
class TestMongoDBChunkStorage:
    """Test MongoDB chunk storage and retrieval."""

    async def test_store_and_retrieve_single_chunk(self, clean_db):
        """Test storing and retrieving a single audio chunk."""
        conversation_id = "test-conv-001"
        pcm_data = generate_pcm_data(duration_seconds=10)
        opus_data = await encode_pcm_to_opus(pcm_data)

        # Create and save chunk
        chunk = AudioChunkDocument(
            conversation_id=conversation_id,
            chunk_index=0,
            audio_data=Binary(opus_data),
            original_size=len(pcm_data),
            compressed_size=len(opus_data),
            start_time=0.0,
            end_time=10.0,
            duration=10.0,
            sample_rate=16000,
            channels=1,
        )
        await chunk.insert()

        # Retrieve chunk
        chunks = await retrieve_audio_chunks(conversation_id)

        assert len(chunks) == 1
        assert chunks[0].conversation_id == conversation_id
        assert chunks[0].chunk_index == 0
        assert len(chunks[0].audio_data) == len(opus_data)

    async def test_retrieve_multiple_chunks_in_order(self, clean_db):
        """Test retrieving multiple chunks in correct order."""
        conversation_id = "test-conv-002"
        num_chunks = 5

        # Create chunks in reverse order
        for i in range(num_chunks - 1, -1, -1):
            pcm_data = generate_pcm_data(duration_seconds=10)
            opus_data = await encode_pcm_to_opus(pcm_data)

            chunk = AudioChunkDocument(
                conversation_id=conversation_id,
                chunk_index=i,
                audio_data=Binary(opus_data),
                original_size=len(pcm_data),
                compressed_size=len(opus_data),
                start_time=float(i * 10),
                end_time=float((i + 1) * 10),
                duration=10.0,
                sample_rate=16000,
                channels=1,
            )
            await chunk.insert()

        # Retrieve all chunks
        chunks = await retrieve_audio_chunks(conversation_id)

        assert len(chunks) == num_chunks
        # Verify sorted by chunk_index
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    async def test_retrieve_chunks_with_pagination(self, clean_db):
        """Test chunk retrieval with start_index and limit."""
        conversation_id = "test-conv-003"

        # Create 10 chunks
        for i in range(10):
            pcm_data = generate_pcm_data(duration_seconds=10)
            opus_data = await encode_pcm_to_opus(pcm_data)

            chunk = AudioChunkDocument(
                conversation_id=conversation_id,
                chunk_index=i,
                audio_data=Binary(opus_data),
                original_size=len(pcm_data),
                compressed_size=len(opus_data),
                start_time=float(i * 10),
                end_time=float((i + 1) * 10),
                duration=10.0,
            )
            await chunk.insert()

        # Retrieve chunks 5-7 (3 chunks starting at index 5)
        chunks = await retrieve_audio_chunks(conversation_id, start_index=5, limit=3)

        assert len(chunks) == 3
        assert chunks[0].chunk_index == 5
        assert chunks[1].chunk_index == 6
        assert chunks[2].chunk_index == 7


@pytest.mark.asyncio
class TestWAVReconstruction:
    """Test complete WAV reconstruction from MongoDB chunks."""

    async def test_reconstruct_wav_from_single_chunk(self, clean_db):
        """Test reconstructing WAV from a single chunk."""
        conversation_id = "test-conv-004"
        pcm_data = generate_pcm_data(duration_seconds=10)
        opus_data = await encode_pcm_to_opus(pcm_data)

        # Store chunk
        chunk = AudioChunkDocument(
            conversation_id=conversation_id,
            chunk_index=0,
            audio_data=Binary(opus_data),
            original_size=len(pcm_data),
            compressed_size=len(opus_data),
            start_time=0.0,
            end_time=10.0,
            duration=10.0,
        )
        await chunk.insert()

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

        # Store 3 chunks (30 seconds total)
        for i in range(num_chunks):
            pcm_data = generate_pcm_data(duration_seconds=10)
            opus_data = await encode_pcm_to_opus(pcm_data)

            chunk = AudioChunkDocument(
                conversation_id=conversation_id,
                chunk_index=i,
                audio_data=Binary(opus_data),
                original_size=len(pcm_data),
                compressed_size=len(opus_data),
                start_time=float(i * 10),
                end_time=float((i + 1) * 10),
                duration=10.0,
            )
            await chunk.insert()

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
        with pytest.raises(ValueError, match="No audio chunks found"):
            await reconstruct_wav_from_conversation("nonexistent-conv")


@pytest.mark.asyncio
class TestWAVConversion:
    """Test WAV file to MongoDB chunk conversion."""

    async def test_convert_wav_to_chunks(self, clean_db, tmp_path):
        """Test converting WAV file to MongoDB chunks."""
        conversation_id = "test-conv-006"

        # Create test WAV file (1 second)
        pcm_data = generate_pcm_data(duration_seconds=1)
        wav_path = tmp_path / "test.wav"
        create_wav_file(pcm_data, wav_path)

        # Create conversation
        conversation = Conversation(
            conversation_id=conversation_id,
            audio_uuid="test-audio-001",
            user_id="test-user",
            client_id="test-client",
        )
        await conversation.insert()

        # Convert to chunks
        num_chunks = await convert_wav_to_chunks(conversation_id, wav_path)

        assert num_chunks == 1  # 1 second = 1 chunk (10s chunks)

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

        # Create conversation
        conversation = Conversation(
            conversation_id=conversation_id,
            audio_uuid="test-audio-002",
            user_id="test-user",
            client_id="test-client",
        )
        await conversation.insert()

        # Convert to chunks
        num_chunks = await convert_wav_to_chunks(conversation_id, wav_path)

        assert num_chunks == 3  # 25 seconds = 3 chunks (0-10s, 10-20s, 20-25s)

        # Verify all chunks stored
        chunks = await retrieve_audio_chunks(conversation_id)
        assert len(chunks) == 3


@pytest.mark.asyncio
class TestChunkWaiting:
    """Test waiting for MongoDB chunks to become available."""

    async def test_wait_for_chunks_immediate_success(self, clean_db):
        """Test wait succeeds when chunks already exist."""
        conversation_id = "test-conv-008"
        pcm_data = generate_pcm_data(duration_seconds=10)
        opus_data = await encode_pcm_to_opus(pcm_data)

        # Create chunk
        chunk = AudioChunkDocument(
            conversation_id=conversation_id,
            chunk_index=0,
            audio_data=Binary(opus_data),
            original_size=len(pcm_data),
            compressed_size=len(opus_data),
            start_time=0.0,
            end_time=10.0,
            duration=10.0,
        )
        await chunk.insert()

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
