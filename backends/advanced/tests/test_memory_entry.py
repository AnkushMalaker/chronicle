"""Unit tests for MemoryEntry dataclass.

Tests timestamp initialization, auto-population, and serialization behavior.
"""

import time
from advanced_omi_backend.services.memory.base import MemoryEntry


class TestMemoryEntryTimestamps:
    """Test MemoryEntry timestamp handling."""

    def test_memory_entry_auto_initializes_timestamps(self):
        """Test that MemoryEntry auto-initializes created_at and updated_at when not provided."""
        before_creation = int(time.time())

        entry = MemoryEntry(
            id="test-123",
            content="Test memory content"
        )

        after_creation = int(time.time())

        # Both timestamps should be set
        assert entry.created_at is not None, "created_at should be auto-initialized"
        assert entry.updated_at is not None, "updated_at should be auto-initialized"

        # Timestamps should be strings
        assert isinstance(entry.created_at, str), "created_at should be a string"
        assert isinstance(entry.updated_at, str), "updated_at should be a string"

        # Timestamps should be numeric (Unix timestamps)
        created_timestamp = int(entry.created_at)
        updated_timestamp = int(entry.updated_at)

        # Timestamps should be within reasonable range (during test execution)
        assert before_creation <= created_timestamp <= after_creation, "created_at should be within test execution time"
        assert before_creation <= updated_timestamp <= after_creation, "updated_at should be within test execution time"

        # Both should be equal since they're created at the same time
        assert entry.created_at == entry.updated_at, "created_at and updated_at should be equal for new entries"

    def test_memory_entry_with_created_at_only(self):
        """Test that updated_at defaults to created_at when only created_at is provided."""
        custom_timestamp = "1234567890"

        entry = MemoryEntry(
            id="test-123",
            content="Test memory content",
            created_at=custom_timestamp
        )

        assert entry.created_at == custom_timestamp, "created_at should match provided value"
        assert entry.updated_at == custom_timestamp, "updated_at should default to created_at"

    def test_memory_entry_with_both_timestamps(self):
        """Test that both timestamps are preserved when explicitly provided."""
        created_timestamp = "1234567890"
        updated_timestamp = "1234567900"

        entry = MemoryEntry(
            id="test-123",
            content="Test memory content",
            created_at=created_timestamp,
            updated_at=updated_timestamp
        )

        assert entry.created_at == created_timestamp, "created_at should match provided value"
        assert entry.updated_at == updated_timestamp, "updated_at should match provided value"
        assert entry.created_at != entry.updated_at, "timestamps should be different when explicitly set"

    def test_memory_entry_to_dict_includes_timestamps(self):
        """Test that to_dict() serialization includes both timestamp fields."""
        entry = MemoryEntry(
            id="test-123",
            content="Test memory content",
            metadata={"user_id": "user-456"}
        )

        entry_dict = entry.to_dict()

        # Verify all expected keys are present
        assert "id" in entry_dict, "Dict should contain 'id'"
        assert "memory" in entry_dict, "Dict should contain 'memory' (for frontend)"
        assert "content" in entry_dict, "Dict should contain 'content'"
        assert "created_at" in entry_dict, "Dict should contain 'created_at'"
        assert "updated_at" in entry_dict, "Dict should contain 'updated_at'"
        assert "metadata" in entry_dict, "Dict should contain 'metadata'"
        assert "user_id" in entry_dict, "Dict should contain 'user_id' (extracted from metadata)"

        # Verify timestamp values are present and correct
        assert entry_dict["created_at"] == entry.created_at, "Serialized created_at should match entry"
        assert entry_dict["updated_at"] == entry.updated_at, "Serialized updated_at should match entry"

        # Verify frontend compatibility
        assert entry_dict["memory"] == entry.content, "memory field should match content for frontend"
        assert entry_dict["content"] == entry.content, "content field should match content"

    def test_memory_entry_with_none_timestamps(self):
        """Test that None timestamps are properly initialized."""
        entry = MemoryEntry(
            id="test-123",
            content="Test memory content",
            created_at=None,
            updated_at=None
        )

        # Both should be auto-initialized even when explicitly set to None
        assert entry.created_at is not None, "created_at should be auto-initialized from None"
        assert entry.updated_at is not None, "updated_at should be auto-initialized from None"
        assert entry.created_at == entry.updated_at, "Both timestamps should be equal when auto-initialized"

    def test_memory_entry_with_all_fields(self):
        """Test MemoryEntry with all fields populated."""
        entry = MemoryEntry(
            id="test-123",
            content="Test memory content",
            metadata={"user_id": "user-456", "source": "test"},
            embedding=[0.1, 0.2, 0.3],
            score=0.95,
            created_at="1234567890",
            updated_at="1234567900"
        )

        # Verify all fields are preserved
        assert entry.id == "test-123"
        assert entry.content == "Test memory content"
        assert entry.metadata == {"user_id": "user-456", "source": "test"}
        assert entry.embedding == [0.1, 0.2, 0.3]
        assert entry.score == 0.95
        assert entry.created_at == "1234567890"
        assert entry.updated_at == "1234567900"

        # Verify serialization
        entry_dict = entry.to_dict()
        assert entry_dict["score"] == 0.95
        assert entry_dict["user_id"] == "user-456"

    def test_memory_entry_timestamp_format(self):
        """Test that timestamps are in the expected format (Unix timestamp strings)."""
        entry = MemoryEntry(
            id="test-123",
            content="Test memory content"
        )

        # Timestamps should be strings representing Unix timestamps
        assert entry.created_at.isdigit(), "created_at should be a numeric string"
        assert entry.updated_at.isdigit(), "updated_at should be a numeric string"

        # Should be parseable as integers
        created_int = int(entry.created_at)
        updated_int = int(entry.updated_at)

        # Should be recent timestamps (after year 2020, before year 2100)
        assert created_int > 1577836800, "Timestamp should be after 2020"
        assert created_int < 4102444800, "Timestamp should be before 2100"
        assert updated_int > 1577836800, "Timestamp should be after 2020"
        assert updated_int < 4102444800, "Timestamp should be before 2100"
