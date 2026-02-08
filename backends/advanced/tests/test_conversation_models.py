"""
Test suite for conversation models.
"""

import pytest
from datetime import datetime
from advanced_omi_backend.models.conversation import (
    Conversation,
    TranscriptVersion,
    MemoryVersion,
    SpeakerSegment,
    TranscriptProvider,
    MemoryProvider,
    create_conversation
)


class TestConversationModel:
    """Test Conversation Pydantic model."""

    def test_create_conversation_factory(self):
        """Test the create_conversation factory function."""
        conversation = create_conversation(
            conversation_id="test-conv-123",
            audio_uuid="test-audio-456",
            user_id="test-user-789",
            client_id="test-client-abc"
        )

        # Verify basic properties
        assert conversation.conversation_id == "test-conv-123"
        assert conversation.audio_uuid == "test-audio-456"
        assert conversation.user_id == "test-user-789"
        assert conversation.client_id == "test-client-abc"
        assert isinstance(conversation.created_at, datetime)

        # Verify defaults
        assert len(conversation.transcript_versions) == 0
        assert len(conversation.memory_versions) == 0
        assert conversation.active_transcript_version is None
        assert conversation.active_memory_version is None
        assert conversation.transcript is None
        assert len(conversation.segments) == 0
        assert len(conversation.memories) == 0
        assert conversation.memory_count == 0

    def test_speaker_segment_model(self):
        """Test SpeakerSegment model."""
        segment = SpeakerSegment(
            start=10.5,
            end=15.8,
            text="Hello, how are you today?",
            speaker="Speaker A",
            confidence=0.95
        )

        assert segment.start == 10.5
        assert segment.end == 15.8
        assert segment.text == "Hello, how are you today?"
        assert segment.speaker == "Speaker A"
        assert segment.confidence == 0.95

    def test_transcript_version_model(self):
        """Test TranscriptVersion model."""
        segments = [
            SpeakerSegment(start=0.0, end=5.0, text="Hello", speaker="Speaker A"),
            SpeakerSegment(start=5.1, end=10.0, text="Hi there", speaker="Speaker B")
        ]

        version = TranscriptVersion(
            version_id="trans-v1",
            transcript="Hello Hi there",
            segments=segments,
            provider=TranscriptProvider.DEEPGRAM,
            model="nova-3",
            created_at=datetime.now(),
            processing_time_seconds=12.5,
            metadata={"confidence": 0.9}
        )

        assert version.version_id == "trans-v1"
        assert version.transcript == "Hello Hi there"
        assert len(version.segments) == 2
        assert version.provider == TranscriptProvider.DEEPGRAM
        assert version.model == "nova-3"
        assert version.processing_time_seconds == 12.5
        assert version.metadata["confidence"] == 0.9

    def test_memory_version_model(self):
        """Test MemoryVersion model."""
        version = MemoryVersion(
            version_id="mem-v1",
            memory_count=5,
            transcript_version_id="trans-v1",
            provider=MemoryProvider.CHRONICLE,
            model="gpt-4o-mini",
            created_at=datetime.now(),
            processing_time_seconds=45.2,
            metadata={"extraction_quality": "high"}
        )

        assert version.version_id == "mem-v1"
        assert version.memory_count == 5
        assert version.transcript_version_id == "trans-v1"
        assert version.provider == MemoryProvider.CHRONICLE
        assert version.model == "gpt-4o-mini"
        assert version.processing_time_seconds == 45.2
        assert version.metadata["extraction_quality"] == "high"

    def test_add_transcript_version(self):
        """Test adding transcript versions to conversation."""
        conversation = create_conversation("conv-1", "audio-1", "user-1", "client-1")

        segments = [SpeakerSegment(start=0.0, end=5.0, text="Test", speaker="Speaker A")]

        # Add first transcript version
        version1 = conversation.add_transcript_version(
            version_id="v1",
            transcript="Test transcript",
            segments=segments,
            provider=TranscriptProvider.DEEPGRAM,
            model="nova-3",
            processing_time_seconds=10.0
        )

        assert len(conversation.transcript_versions) == 1
        assert conversation.active_transcript_version == "v1"
        assert conversation.transcript == "Test transcript"
        assert len(conversation.segments) == 1
        assert version1.version_id == "v1"

        # Add second transcript version without setting as active
        version2 = conversation.add_transcript_version(
            version_id="v2",
            transcript="Updated transcript",
            segments=segments,
            provider=TranscriptProvider.PARAKEET,
            set_as_active=False
        )

        assert len(conversation.transcript_versions) == 2
        assert conversation.active_transcript_version == "v1"  # Still v1
        assert conversation.transcript == "Test transcript"  # Still v1 content

    def test_add_memory_version(self):
        """Test adding memory versions to conversation."""
        conversation = create_conversation("conv-1", "audio-1", "user-1", "client-1")

        # Add memory version
        version1 = conversation.add_memory_version(
            version_id="m1",
            memory_count=3,
            transcript_version_id="v1",
            provider=MemoryProvider.CHRONICLE,
            model="gpt-4o-mini",
            processing_time_seconds=30.0
        )

        assert len(conversation.memory_versions) == 1
        assert conversation.active_memory_version == "m1"
        assert conversation.memory_count == 3
        assert version1.version_id == "m1"

    def test_set_active_versions(self):
        """Test switching between active versions."""
        conversation = create_conversation("conv-1", "audio-1", "user-1", "client-1")

        # Add two transcript versions
        segments1 = [SpeakerSegment(start=0.0, end=5.0, text="Version 1", speaker="Speaker A")]
        segments2 = [SpeakerSegment(start=0.0, end=5.0, text="Version 2", speaker="Speaker A")]

        conversation.add_transcript_version("v1", "Transcript 1", segments1, TranscriptProvider.DEEPGRAM)
        conversation.add_transcript_version("v2", "Transcript 2", segments2, TranscriptProvider.PARAKEET, set_as_active=False)

        # Should be v1 active
        assert conversation.active_transcript_version == "v1"
        assert conversation.transcript == "Transcript 1"

        # Switch to v2
        success = conversation.set_active_transcript_version("v2")
        assert success is True
        assert conversation.active_transcript_version == "v2"
        assert conversation.transcript == "Transcript 2"

        # Try to switch to non-existent version
        success = conversation.set_active_transcript_version("v999")
        assert success is False
        assert conversation.active_transcript_version == "v2"  # Unchanged

    def test_active_version_properties(self):
        """Test active version property methods."""
        conversation = create_conversation("conv-1", "audio-1", "user-1", "client-1")

        # No active versions initially
        assert conversation.active_transcript is None
        assert conversation.active_memory is None

        # Add versions
        segments = [SpeakerSegment(start=0.0, end=5.0, text="Test", speaker="Speaker A")]
        conversation.add_transcript_version("v1", "Test", segments, TranscriptProvider.DEEPGRAM)
        conversation.add_memory_version("m1", 2, "v1", MemoryProvider.CHRONICLE)

        # Should return active versions
        active_transcript = conversation.active_transcript
        active_memory = conversation.active_memory

        assert active_transcript is not None
        assert active_transcript.version_id == "v1"
        assert active_memory is not None
        assert active_memory.version_id == "m1"

    def test_provider_enums(self):
        """Test that provider enums work correctly."""
        # Test TranscriptProvider enum
        assert TranscriptProvider.DEEPGRAM == "deepgram"
        assert TranscriptProvider.PARAKEET == "parakeet"

        # Test MemoryProvider enum
        assert MemoryProvider.CHRONICLE == "chronicle"
        assert MemoryProvider.OPENMEMORY_MCP == "openmemory_mcp"

    def test_conversation_model_dump(self):
        """Test that Conversation can be serialized for MongoDB storage."""
        conversation = create_conversation("conv-1", "audio-1", "user-1", "client-1")

        # Add some versions
        segments = [SpeakerSegment(start=0.0, end=5.0, text="Test", speaker="Speaker A")]
        conversation.add_transcript_version("v1", "Test", segments, TranscriptProvider.DEEPGRAM)
        conversation.add_memory_version("m1", 2, "v1", MemoryProvider.CHRONICLE)

        # Test model_dump() works
        conv_dict = conversation.model_dump()

        # Verify essential fields are present
        assert "conversation_id" in conv_dict
        assert "audio_uuid" in conv_dict
        assert "user_id" in conv_dict
        assert "client_id" in conv_dict
        assert "created_at" in conv_dict
        assert "transcript_versions" in conv_dict
        assert "memory_versions" in conv_dict
        assert "active_transcript_version" in conv_dict
        assert "active_memory_version" in conv_dict

        # Verify nested structures
        assert len(conv_dict["transcript_versions"]) == 1
        assert len(conv_dict["memory_versions"]) == 1
        assert conv_dict["active_transcript_version"] == "v1"
        assert conv_dict["active_memory_version"] == "m1"

    def test_conversation_recreation_from_dict(self):
        """Test that Conversation can be recreated from a dict."""
        # Create original conversation
        original = create_conversation("conv-1", "audio-1", "user-1", "client-1")
        segments = [SpeakerSegment(start=0.0, end=5.0, text="Test", speaker="Speaker A")]
        original.add_transcript_version("v1", "Test", segments, TranscriptProvider.DEEPGRAM)

        # Convert to dict and back
        conv_dict = original.model_dump()
        recreated = Conversation(**conv_dict)

        # Verify they match
        assert recreated.conversation_id == original.conversation_id
        assert recreated.audio_uuid == original.audio_uuid
        assert recreated.user_id == original.user_id
        assert recreated.active_transcript_version == original.active_transcript_version
        assert len(recreated.transcript_versions) == len(original.transcript_versions)
        assert recreated.transcript == original.transcript