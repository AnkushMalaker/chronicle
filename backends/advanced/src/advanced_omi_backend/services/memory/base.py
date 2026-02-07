"""Abstract base classes for the memory service architecture.

This module defines the core abstractions and interfaces for:
- Memory service operations
- LLM provider integration
- Vector store backends
- Memory entry data structures

All concrete implementations should inherit from these base classes.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["MemoryEntry", "MemoryServiceBase", "LLMProviderBase", "VectorStoreBase"]


@dataclass
class MemoryEntry:
    """Represents a memory entry with content, metadata, and embeddings.

    This is the core data structure used throughout the memory service
    for storing and retrieving user memories.

    Attributes:
        id: Unique identifier for the memory
        content: The actual memory text/content
        metadata: Additional metadata (user_id, source, timestamps, etc.)
        embedding: Vector embedding for semantic search (optional)
        score: Similarity score from search operations (optional)
        created_at: Timestamp when memory was created
        updated_at: Timestamp when memory was last updated
    """

    id: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    score: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self):
        """Set created_at and updated_at timestamps if not provided."""
        current_time = str(int(time.time()))
        if self.created_at is None:
            self.created_at = current_time
        if self.updated_at is None:
            self.updated_at = self.created_at  # Default to created_at, not current_time

    def to_dict(self) -> Dict[str, Any]:
        """Convert MemoryEntry to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "memory": self.content,  # Frontend expects 'memory' key
            "content": self.content,  # Also provide 'content' for consistency
            "metadata": self.metadata,
            "embedding": self.embedding,
            "score": self.score,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "user_id": self.metadata.get("user_id"),  # Extract user_id from metadata
        }


class MemoryServiceBase(ABC):
    """Abstract base class defining the core memory service interface.

    This class defines all the essential operations that any memory service
    implementation must provide. Concrete implementations should inherit
    from this class and implement all abstract methods.
    """

    @property
    @abstractmethod
    def provider_identifier(self) -> str:
        """Return the provider identifier (e.g., 'chronicle', 'openmemory_mcp', 'mycelia')."""
        ...

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the memory service and all its components.

        This should set up connections to LLM providers, vector stores,
        and any other required dependencies.

        Raises:
            RuntimeError: If initialization fails
        """
        pass

    @abstractmethod
    async def add_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        allow_update: bool = False,
        db_helper: Any = None,
    ) -> Tuple[bool, List[str]]:
        """Add memories extracted from a transcript.

        Args:
            transcript: Raw transcript text to extract memories from
            client_id: Client identifier
            source_id: Unique identifier for the source (audio session, chat session, etc.)
            user_id: User identifier
            user_email: User email address
            allow_update: Whether to allow updating existing memories
            db_helper: Optional database helper for tracking relationships

        Returns:
            Tuple of (success: bool, created_memory_ids: List[str])
        """
        pass

    @abstractmethod
    async def search_memories(
        self, query: str, user_id: str, limit: int = 10, score_threshold: float = 0.0
    ) -> List[MemoryEntry]:
        """Search memories using semantic similarity.

        Args:
            query: Search query text
            user_id: User identifier to filter memories
            limit: Maximum number of results to return
            score_threshold: Minimum similarity score (0.0 = no threshold)

        Returns:
            List of matching MemoryEntry objects ordered by relevance
        """
        pass

    @abstractmethod
    async def get_all_memories(self, user_id: str, limit: int = 100) -> List[MemoryEntry]:
        """Get all memories for a specific user.

        Args:
            user_id: User identifier
            limit: Maximum number of memories to return

        Returns:
            List of MemoryEntry objects for the user
        """
        pass

    async def count_memories(self, user_id: str) -> Optional[int]:
        """Count total number of memories for a user.

        This is an optional method that providers can implement for efficient
        counting. Returns None if the provider doesn't support counting.

        Args:
            user_id: User identifier

        Returns:
            Total count of memories for the user, or None if not supported
        """
        return None

    async def get_memory(
        self, memory_id: str, user_id: Optional[str] = None
    ) -> Optional[MemoryEntry]:
        """Get a specific memory by ID.

        This is an optional method that providers can implement for fetching
        individual memories. Returns None if the provider doesn't support it
        or the memory is not found.

        Args:
            memory_id: Unique identifier of the memory to retrieve
            user_id: Optional user ID for authentication/filtering

        Returns:
            MemoryEntry object if found, None otherwise
        """
        return None

    async def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        """Update a specific memory's content and/or metadata.

        This is an optional method that providers can implement for updating
        existing memories. Returns False if not supported or update fails.

        Args:
            memory_id: Unique identifier of the memory to update
            content: New content for the memory (if None, content is not updated)
            metadata: New metadata to merge with existing (if None, metadata is not updated)
            user_id: Optional user ID for authentication
            user_email: Optional user email for authentication

        Returns:
            True if update succeeded, False otherwise
        """
        return False

    @abstractmethod
    async def delete_memory(
        self, memory_id: str, user_id: Optional[str] = None, user_email: Optional[str] = None
    ) -> bool:
        """Delete a specific memory by ID.

        Args:
            memory_id: Unique identifier of the memory to delete
            user_id: Optional user ID for authentication (required for Mycelia provider)
            user_email: Optional user email for authentication (required for Mycelia provider)

        Returns:
            True if successfully deleted, False otherwise
        """
        pass

    @abstractmethod
    async def delete_all_user_memories(self, user_id: str) -> int:
        """Delete all memories for a specific user.

        Args:
            user_id: User identifier

        Returns:
            Number of memories that were deleted
        """
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """Test if the memory service and its dependencies are working.

        Returns:
            True if all connections are healthy, False otherwise
        """
        pass

    def shutdown(self) -> None:
        """Shutdown the memory service and clean up resources.

        Default implementation does nothing. Subclasses should override
        if they need to perform cleanup operations.
        """
        pass

    def __init__(self):
        """Initialize base memory service state.

        Subclasses should call super().__init__() in their constructors.
        """
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the memory service is initialized before use.

        This method provides lazy initialization - it will automatically
        call initialize() the first time it's needed. This is critical
        for services used in RQ workers where the service instance is
        created in one process but used in another.

        This should be called at the start of any method that requires
        the service to be initialized (e.g., add_memory, search_memories).
        """
        if not self._initialized:
            await self.initialize()


class LLMProviderBase(ABC):
    """Abstract base class for LLM provider implementations.

    LLM providers handle:
    - Memory extraction from text using prompts
    - Text embedding generation
    - Memory action proposals (add/update/delete decisions)
    """

    @abstractmethod
    async def extract_memories(self, text: str, prompt: str) -> List[str]:
        """Extract meaningful fact memories from text using an LLM.

        Args:
            text: Input text to extract memories from
            prompt: System prompt to guide the extraction process

        Returns:
            List of extracted fact memory strings
        """
        pass

    @abstractmethod
    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate vector embeddings for the given texts.

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors (one per input text)
        """
        pass

    @abstractmethod
    async def propose_memory_actions(
        self,
        retrieved_old_memory: List[Dict[str, str]],
        new_facts: List[str],
        custom_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Propose memory management actions based on existing and new information.

        This method uses the LLM to decide whether new facts should:
        - ADD: Create new memories
        - UPDATE: Modify existing memories
        - DELETE: Remove outdated memories
        - NONE: No action needed

        Args:
            retrieved_old_memory: List of existing memories for context
            new_facts: List of new facts to process
            custom_prompt: Optional custom prompt to use instead of default

        Returns:
            Dictionary containing proposed actions in structured format
        """
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """Test connection to the LLM provider.

        Returns:
            True if connection is working, False otherwise
        """
        pass


class VectorStoreBase(ABC):
    """Abstract base class for vector store implementations.

    Vector stores handle:
    - Storing memory embeddings with metadata
    - Semantic search using vector similarity
    - CRUD operations on memory entries
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the vector store (create collections, etc.).

        Raises:
            RuntimeError: If initialization fails
        """
        pass

    @abstractmethod
    async def add_memories(self, memories: List[MemoryEntry]) -> List[str]:
        """Add multiple memory entries to the vector store.

        Args:
            memories: List of MemoryEntry objects to store

        Returns:
            List of created memory IDs
        """
        pass

    @abstractmethod
    async def search_memories(
        self, query_embedding: List[float], user_id: str, limit: int, score_threshold: float = 0.0
    ) -> List[MemoryEntry]:
        """Search memories using vector similarity.

        Args:
            query_embedding: Query vector for similarity search
            user_id: User identifier to filter results
            limit: Maximum number of results to return
            score_threshold: Minimum similarity score (0.0 = no threshold)

        Returns:
            List of matching MemoryEntry objects with similarity scores
        """
        pass

    @abstractmethod
    async def get_memories(self, user_id: str, limit: int) -> List[MemoryEntry]:
        """Get all memories for a user without similarity filtering.

        Args:
            user_id: User identifier
            limit: Maximum number of memories to return

        Returns:
            List of MemoryEntry objects for the user
        """
        pass

    async def count_memories(self, user_id: str) -> Optional[int]:
        """Count total number of memories for a user.

        Default implementation returns None to indicate counting is unsupported.
        Vector stores should override this method to provide efficient counting if supported.

        Args:
            user_id: User identifier

        Returns:
            Total count of memories for the user, or None if counting is not supported by this store
        """
        return None

    @abstractmethod
    async def update_memory(
        self,
        memory_id: str,
        new_content: str,
        new_embedding: List[float],
        new_metadata: Dict[str, Any],
    ) -> bool:
        """Update an existing memory with new content and metadata.

        Args:
            memory_id: ID of the memory to update
            new_content: Updated memory content
            new_embedding: Updated embedding vector
            new_metadata: Updated metadata

        Returns:
            True if update succeeded, False otherwise
        """
        pass

    @abstractmethod
    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a specific memory from the store.

        Args:
            memory_id: ID of the memory to delete

        Returns:
            True if deletion succeeded, False otherwise
        """
        pass

    @abstractmethod
    async def delete_user_memories(self, user_id: str) -> int:
        """Delete all memories for a specific user.

        Args:
            user_id: User identifier

        Returns:
            Number of memories that were deleted
        """
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """Test connection to the vector store.

        Returns:
            True if connection is working, False otherwise
        """
        pass
