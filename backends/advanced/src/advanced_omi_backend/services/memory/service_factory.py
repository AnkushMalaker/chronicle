"""Memory service factory for creating appropriate memory service instances.

This module provides a factory pattern for instantiating memory services
based on configuration. It supports both the sophisticated Chronicle
implementation and the OpenMemory MCP backend.
"""

import asyncio
import logging
import threading
from typing import Optional

from .base import MemoryServiceBase
from .config import MemoryConfig, MemoryProvider, build_memory_config_from_env

memory_logger = logging.getLogger("memory_service")

# Global memory service instance
_memory_service: Optional[MemoryServiceBase] = None
# Lock for thread-safe singleton creation
_memory_service_lock = threading.Lock()


def create_memory_service(config: MemoryConfig) -> MemoryServiceBase:
    """Create a memory service instance based on configuration.

    Args:
        config: Memory service configuration

    Returns:
        Configured memory service instance

    Raises:
        ValueError: If unsupported memory provider is specified
        RuntimeError: If required dependencies are missing
    """
    memory_logger.info(f"🧠  Creating memory service with provider: {config.memory_provider.value}")

    if config.memory_provider == MemoryProvider.CHRONICLE:
        # Use the sophisticated Chronicle implementation
        from .providers.chronicle import MemoryService as ChronicleMemoryService

        return ChronicleMemoryService(config)

    elif config.memory_provider == MemoryProvider.OPENMEMORY_MCP:
        # Use OpenMemory MCP implementation
        try:
            from .providers.openmemory_mcp import OpenMemoryMCPService
        except ImportError as e:
            raise RuntimeError(f"OpenMemory MCP service not available: {e}")

        if not config.openmemory_config:
            raise ValueError("OpenMemory configuration is required for OPENMEMORY_MCP provider")

        return OpenMemoryMCPService(**config.openmemory_config)

    else:
        raise ValueError(f"Unsupported memory provider: {config.memory_provider}")


def get_memory_service() -> MemoryServiceBase:
    """Get the global memory service instance.

    This function implements the singleton pattern and will create the
    memory service on first access based on environment configuration.

    Returns:
        Memory service instance that is created lazily on first use
        and may return an already-initialized singleton

    Raises:
        RuntimeError: If memory service creation or initialization fails
    """
    global _memory_service

    # Double-checked locking pattern for thread-safe singleton creation
    if _memory_service is None:
        with _memory_service_lock:
            # Re-check after acquiring lock in case another thread created it
            if _memory_service is None:
                try:
                    # Build configuration from environment
                    config = build_memory_config_from_env()

                    # Create appropriate service implementation
                    _memory_service = create_memory_service(config)

                    # Don't initialize here - let it happen lazily on first use
                    # This prevents orphaned tasks that cause "Task was destroyed but it is pending" errors
                    memory_logger.debug(
                        f"Memory service created but not initialized: {type(_memory_service).__name__}"
                    )

                    memory_logger.info(
                        f"✅ Global memory service created: {type(_memory_service).__name__}"
                    )

                except Exception as e:
                    memory_logger.error(f"❌ Failed to create memory service: {e}")
                    raise RuntimeError(f"Memory service creation failed: {e}")

    return _memory_service


def shutdown_memory_service() -> None:
    """Shutdown the global memory service and clean up resources."""
    global _memory_service

    if _memory_service is not None:
        try:
            _memory_service.shutdown()
            memory_logger.info("🔄 Memory service shut down")
        except Exception as e:
            memory_logger.error(f"Error shutting down memory service: {e}")
        finally:
            _memory_service = None


def reset_memory_service() -> None:
    """Reset the global memory service (useful for testing)."""
    global _memory_service
    if _memory_service is not None:
        shutdown_memory_service()
    _memory_service = None
    memory_logger.info("🔄 Memory service reset")


def get_service_info() -> dict:
    """Get information about the current memory service.

    Returns:
        Dictionary with service information
    """
    global _memory_service

    info = {
        "service_created": _memory_service is not None,
        "service_type": None,
        "service_initialized": False,
        "memory_provider": None,
    }

    if _memory_service is not None:
        info["service_type"] = type(_memory_service).__name__
        # All memory services should have _initialized attribute per the base class
        info["service_initialized"] = _memory_service._initialized

        info["memory_provider"] = _memory_service.provider_identifier

    return info
