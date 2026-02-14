"""
Test suite for conversation models.

Tests that don't need Beanie initialization (pure model validation).
"""

from datetime import datetime

from advanced_omi_backend.models.conversation import Conversation


class TestConversationModel:
    """Test Conversation Pydantic model (no DB required)."""

    def test_speaker_segment_model(self):
        """Test SpeakerSegment model."""
        segment = Conversation.SpeakerSegment(
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
            Conversation.SpeakerSegment(start=0.0, end=5.0, text="Hello", speaker="Speaker A"),
            Conversation.SpeakerSegment(start=5.1, end=10.0, text="Hi there", speaker="Speaker B")
        ]

        version = Conversation.TranscriptVersion(
            version_id="trans-v1",
            transcript="Hello Hi there",
            segments=segments,
            provider="deepgram",
            model="nova-3",
            created_at=datetime.now(),
            processing_time_seconds=12.5,
            metadata={"confidence": 0.9}
        )

        assert version.version_id == "trans-v1"
        assert version.transcript == "Hello Hi there"
        assert len(version.segments) == 2
        assert version.provider == "deepgram"
        assert version.model == "nova-3"
        assert version.processing_time_seconds == 12.5
        assert version.metadata["confidence"] == 0.9

    def test_memory_version_model(self):
        """Test MemoryVersion model."""
        version = Conversation.MemoryVersion(
            version_id="mem-v1",
            memory_count=5,
            transcript_version_id="trans-v1",
            provider=Conversation.MemoryProvider.CHRONICLE,
            model="gpt-4o-mini",
            created_at=datetime.now(),
            processing_time_seconds=45.2,
            metadata={"extraction_quality": "high"}
        )

        assert version.version_id == "mem-v1"
        assert version.memory_count == 5
        assert version.transcript_version_id == "trans-v1"
        assert version.provider == Conversation.MemoryProvider.CHRONICLE
        assert version.model == "gpt-4o-mini"
        assert version.processing_time_seconds == 45.2
        assert version.metadata["extraction_quality"] == "high"

    def test_provider_enums(self):
        """Test that provider enums work correctly."""
        assert Conversation.MemoryProvider.CHRONICLE == "chronicle"
        assert Conversation.MemoryProvider.OPENMEMORY_MCP == "openmemory_mcp"

    def test_word_model(self):
        """Test Word model."""
        word = Conversation.Word(
            word="hello",
            start=0.0,
            end=0.5,
            confidence=0.98
        )
        assert word.word == "hello"
        assert word.start == 0.0
        assert word.end == 0.5
        assert word.confidence == 0.98

    def test_speaker_segment_defaults(self):
        """Test SpeakerSegment default values."""
        segment = Conversation.SpeakerSegment(
            start=0.0,
            end=1.0,
            text="Test",
            speaker="Speaker 0"
        )
        assert segment.confidence is None
        assert segment.identified_as is None
        assert segment.words == []

    def test_transcript_version_defaults(self):
        """Test TranscriptVersion default values."""
        version = Conversation.TranscriptVersion(
            version_id="v1",
            created_at=datetime.now(),
        )
        assert version.transcript is None
        assert version.words == []
        assert version.segments == []
        assert version.provider is None
        assert version.model is None
        assert version.processing_time_seconds is None
        assert version.metadata == {}
