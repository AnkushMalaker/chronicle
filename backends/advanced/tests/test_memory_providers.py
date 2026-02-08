"""Unit tests for memory provider timestamp handling.

Tests that all providers properly handle created_at and updated_at fields
when converting their native formats to MemoryEntry objects.
"""

import time
from unittest.mock import Mock
from advanced_omi_backend.services.memory.providers.mycelia import MyceliaMemoryService
from advanced_omi_backend.services.memory.providers.openmemory_mcp import OpenMemoryMCPService
from advanced_omi_backend.services.memory.base import MemoryEntry


class TestMyceliaProviderTimestamps:
    """Test Mycelia provider timestamp handling."""

    def test_mycelia_object_to_memory_entry_with_both_timestamps(self):
        """Test that Mycelia provider extracts both created_at and updated_at."""
        # Create a Mycelia service instance
        service = MyceliaMemoryService(Mock())

        # Mock Mycelia API object response
        mycelia_obj = {
            "_id": {"$oid": "507f1f77bcf86cd799439011"},
            "name": "Test Memory",
            "details": "Test content",
            "createdAt": {"$date": "2024-01-01T00:00:00.000Z"},
            "updatedAt": {"$date": "2024-01-02T00:00:00.000Z"},
            "isPerson": False,
            "isEvent": False,
        }

        # Convert to MemoryEntry
        entry = service._mycelia_object_to_memory_entry(mycelia_obj, user_id="user-123")

        # Verify both timestamps are extracted
        assert entry.created_at is not None, "created_at should be extracted"
        assert entry.updated_at is not None, "updated_at should be extracted"

        # Verify timestamps match the source
        assert entry.created_at == "2024-01-01T00:00:00.000Z", "created_at should match Mycelia createdAt"
        assert entry.updated_at == "2024-01-02T00:00:00.000Z", "updated_at should match Mycelia updatedAt"

        # Verify timestamps are different (updated after created)
        assert entry.created_at != entry.updated_at, "Timestamps should be different"

    def test_mycelia_object_to_memory_entry_with_missing_updated_at(self):
        """Test that Mycelia provider handles missing updatedAt gracefully."""
        service = MyceliaMemoryService(Mock())

        # Mock Mycelia object without updatedAt
        mycelia_obj = {
            "_id": {"$oid": "507f1f77bcf86cd799439011"},
            "name": "Test Memory",
            "details": "Test content",
            "createdAt": {"$date": "2024-01-01T00:00:00.000Z"},
            # updatedAt is missing
            "isPerson": False,
            "isEvent": False,
        }

        # Convert to MemoryEntry
        entry = service._mycelia_object_to_memory_entry(mycelia_obj, user_id="user-123")

        # created_at should be present
        assert entry.created_at is not None, "created_at should be extracted"

        # updated_at should default to created_at (via MemoryEntry __post_init__)
        # The _extract_bson_date returns None for missing fields, then __post_init__ sets it to created_at
        assert entry.updated_at is not None, "updated_at should be set by __post_init__"
        assert entry.updated_at == entry.created_at, "updated_at should default to created_at when missing"

    def test_mycelia_extract_bson_date(self):
        """Test Mycelia BSON date extraction."""
        service = MyceliaMemoryService(Mock())

        # Test BSON date format
        bson_date = {"$date": "2024-01-01T00:00:00.000Z"}
        extracted = service._extract_bson_date(bson_date)
        assert extracted == "2024-01-01T00:00:00.000Z", "Should extract date from BSON format"

        # Test plain string date
        plain_date = "2024-01-01T00:00:00.000Z"
        extracted = service._extract_bson_date(plain_date)
        assert extracted == "2024-01-01T00:00:00.000Z", "Should pass through plain date"

        # Test None
        extracted = service._extract_bson_date(None)
        assert extracted is None, "Should return None for None input"


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
            "metadata": {"source": "test"}
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
        assert entry.created_at == entry.updated_at, "updated_at should equal created_at when missing"

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
        assert entry.content == "Test memory content", "Should extract from 'memory' field"
        assert entry.created_at == "1704067200", "Should extract from 'timestamp' field"
        assert entry.updated_at == "1704153600", "Should extract from 'modified_at' field"

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
        assert before_conversion <= created_int <= after_conversion, "Timestamp should be current"
        assert before_conversion <= updated_int <= after_conversion, "Timestamp should be current"


class TestProviderTimestampConsistency:
    """Test that all providers handle timestamps consistently."""

    def test_all_providers_return_memory_entry_with_timestamps(self):
        """Test that all providers return MemoryEntry objects with both timestamp fields."""
        # This is a meta-test to ensure all providers conform to the MemoryEntry interface

        # Mycelia
        mycelia_service = MyceliaMemoryService(Mock())
        mycelia_obj = {
            "_id": {"$oid": "507f1f77bcf86cd799439011"},
            "name": "Test",
            "details": "Content",
            "createdAt": {"$date": "2024-01-01T00:00:00.000Z"},
            "updatedAt": {"$date": "2024-01-02T00:00:00.000Z"},
        }
        mycelia_entry = mycelia_service._mycelia_object_to_memory_entry(mycelia_obj, "user-123")

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
        for entry, provider_name in [(mycelia_entry, "Mycelia"), (mcp_entry, "OpenMemory MCP")]:
            assert isinstance(entry, MemoryEntry), f"{provider_name} should return MemoryEntry"
            assert hasattr(entry, "created_at"), f"{provider_name} entry should have created_at"
            assert hasattr(entry, "updated_at"), f"{provider_name} entry should have updated_at"
            assert entry.created_at is not None, f"{provider_name} created_at should not be None"
            assert entry.updated_at is not None, f"{provider_name} updated_at should not be None"
