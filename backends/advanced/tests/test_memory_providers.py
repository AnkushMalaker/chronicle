"""Unit tests for memory provider timestamp handling.

Tests that all providers properly handle created_at and updated_at fields
when converting their native formats to MemoryEntry objects.
"""

import time

from advanced_omi_backend.services.memory.base import MemoryEntry
from advanced_omi_backend.services.memory.providers.openmemory_mcp import (
    OpenMemoryMCPService,
)


class TestOpenMemoryMCPProviderTimestamps:
    """Test OpenMemory MCP provider timestamp handling."""

    def test_mcp_result_to_memory_entry_with_both_timestamps(self):
        """Test that OpenMemory MCP provider extracts both timestamps."""
        # Create OpenMemory MCP service instance
        service = OpenMemoryMCPService()
        service.client_name = "test-client"
        service.server_url = "http://localhost:8765"

        # Mock MCP API response
        mcp_result = {
            "id": "mem-123",
            "content": "Test memory content",
            "created_at": "1704067200",  # 2024-01-01 00:00:00 UTC
            "updated_at": "1704153600",  # 2024-01-02 00:00:00 UTC
            "metadata": {"source": "test"},
        }

        # Convert to MemoryEntry
        entry = service._mcp_result_to_memory_entry(mcp_result, user_id="user-123")

        # Verify both timestamps are extracted
        assert entry is not None, "MemoryEntry should be created"
        assert entry.created_at is not None, "created_at should be extracted"
        assert entry.updated_at is not None, "updated_at should be extracted"

        # Verify timestamps match the source
        assert entry.created_at == "1704067200", "created_at should match MCP response"
        assert entry.updated_at == "1704153600", "updated_at should match MCP response"

        # Verify timestamps are different
        assert entry.created_at != entry.updated_at, "Timestamps should be different"

    def test_mcp_result_to_memory_entry_with_missing_updated_at(self):
        """Test that OpenMemory MCP provider defaults updated_at to created_at when missing."""
        service = OpenMemoryMCPService()
        service.client_name = "test-client"
        service.server_url = "http://localhost:8765"

        # Mock MCP response without updated_at
        mcp_result = {
            "id": "mem-123",
            "content": "Test memory content",
            "created_at": "1704067200",
            # updated_at is missing
        }

        # Convert to MemoryEntry
        entry = service._mcp_result_to_memory_entry(mcp_result, user_id="user-123")

        # Verify updated_at defaults to created_at
        assert entry is not None, "MemoryEntry should be created"
        assert entry.created_at is not None, "created_at should be present"
        assert entry.updated_at is not None, "updated_at should default to created_at"
        assert (
            entry.created_at == entry.updated_at
        ), "updated_at should equal created_at when missing"

    def test_mcp_result_to_memory_entry_with_alternate_timestamp_fields(self):
        """Test that OpenMemory MCP provider handles alternate timestamp field names."""
        service = OpenMemoryMCPService()
        service.client_name = "test-client"
        service.server_url = "http://localhost:8765"

        # Mock MCP response with alternate field names
        mcp_result = {
            "id": "mem-123",
            "memory": "Test memory content",  # Alternate content field
            "timestamp": "1704067200",  # Alternate created_at field
            "modified_at": "1704153600",  # Alternate updated_at field
        }

        # Convert to MemoryEntry
        entry = service._mcp_result_to_memory_entry(mcp_result, user_id="user-123")

        # Verify conversion handles alternate field names
        assert entry is not None, "MemoryEntry should be created"
        assert (
            entry.content == "Test memory content"
        ), "Should extract from 'memory' field"
        assert entry.created_at == "1704067200", "Should extract from 'timestamp' field"
        assert (
            entry.updated_at == "1704153600"
        ), "Should extract from 'modified_at' field"

    def test_mcp_result_with_no_timestamps(self):
        """Test that OpenMemory MCP provider generates timestamps when none provided."""
        service = OpenMemoryMCPService()
        service.client_name = "test-client"
        service.server_url = "http://localhost:8765"

        before_conversion = int(time.time())

        # Mock MCP response without any timestamp fields
        mcp_result = {
            "id": "mem-123",
            "content": "Test memory content",
        }

        # Convert to MemoryEntry
        entry = service._mcp_result_to_memory_entry(mcp_result, user_id="user-123")

        after_conversion = int(time.time())

        # Verify timestamps are auto-generated
        assert entry is not None, "MemoryEntry should be created"
        assert entry.created_at is not None, "created_at should be auto-generated"
        assert entry.updated_at is not None, "updated_at should be auto-generated"

        # Verify timestamps are current (within test execution window)
        created_int = int(entry.created_at)
        updated_int = int(entry.updated_at)
        assert (
            before_conversion <= created_int <= after_conversion
        ), "Timestamp should be current"
        assert (
            before_conversion <= updated_int <= after_conversion
        ), "Timestamp should be current"


class TestProviderTimestampConsistency:
    """Test that all providers handle timestamps consistently."""

    def test_all_providers_return_memory_entry_with_timestamps(self):
        """Test that all providers return MemoryEntry objects with both timestamp fields."""
        # OpenMemory MCP
        mcp_service = OpenMemoryMCPService()
        mcp_service.client_name = "test"
        mcp_service.server_url = "http://localhost:8765"
        mcp_result = {
            "id": "mem-123",
            "content": "Content",
            "created_at": "1704067200",
            "updated_at": "1704153600",
        }
        mcp_entry = mcp_service._mcp_result_to_memory_entry(mcp_result, "user-123")

        # Verify all return MemoryEntry instances with both timestamp fields
        for entry, provider_name in [(mcp_entry, "OpenMemory MCP")]:
            assert isinstance(
                entry, MemoryEntry
            ), f"{provider_name} should return MemoryEntry"
            assert hasattr(
                entry, "created_at"
            ), f"{provider_name} entry should have created_at"
            assert hasattr(
                entry, "updated_at"
            ), f"{provider_name} entry should have updated_at"
            assert (
                entry.created_at is not None
            ), f"{provider_name} created_at should not be None"
            assert (
                entry.updated_at is not None
            ), f"{provider_name} updated_at should not be None"
