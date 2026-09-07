"""Memory service package.

This package provides memory management functionality backed by Chronicle's
agentic Markdown vault.

The memory service handles extraction, storage, and retrieval of memories
from user conversations and interactions.

Architecture:
- base.py: Abstract base classes and interfaces
- config.py: Configuration management
- service_factory.py: Provider selection and instantiation
- providers/chronicle.py: Chronicle native provider (agentic Markdown vault)
- providers/llm_providers.py: LLM implementations (OpenAI-compatible)
- vault_manager.py: Conversation document vault (.md file I/O)
"""

import logging

memory_logger = logging.getLogger("memory_service")

# Import the main interface functions from service_factory
from .service_factory import (
    get_memory_service,
    reset_memory_service,
    shutdown_memory_service,
)

__all__ = [
    "get_memory_service",
    "reset_memory_service",
    "shutdown_memory_service",
]
