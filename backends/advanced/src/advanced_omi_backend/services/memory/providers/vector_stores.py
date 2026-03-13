"""Vector store implementations for memory service.

This module provides concrete implementations of vector stores for:
- Qdrant (high-performance vector database)

Vector stores handle storage, retrieval, and similarity search of memory embeddings.
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    Range,
    VectorParams,
)

from ..base import MemoryEntry, VectorStoreBase

memory_logger = logging.getLogger("memory_service")


class QdrantVectorStore(VectorStoreBase):
    """Qdrant vector store implementation.

    Provides high-performance vector storage and similarity search using
    Qdrant database. Handles memory persistence, user isolation, and
    semantic search operations.

    Attributes:
        host: Qdrant server hostname
        port: Qdrant server port
        collection_name: Name of the collection to store memories
        embedding_dims: Dimensionality of the embedding vectors
        client: Qdrant async client instance
    """

    def __init__(self, config: Dict[str, Any]):
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 6333)
        self.collection_name = config.get("collection_name", "memories")
        self.embedding_dims = config.get("embedding_dims", 1536)
        self.client = None

    async def initialize(self) -> None:
        """Initialize Qdrant client and collection.

        Creates the collection if it doesn't exist with appropriate
        vector configuration for cosine similarity search.

        If the collection exists but has different dimensions, it will
        be recreated with the correct dimensions (data will be lost).

        Raises:
            RuntimeError: If initialization fails
        """
        try:
            self.client = AsyncQdrantClient(host=self.host, port=self.port)

            # Check if collection exists and get its info
            collections = await self.client.get_collections()
            collection_exists = any(
                col.name == self.collection_name for col in collections.collections
            )

            need_create = False

            if collection_exists:
                # Check if dimensions match
                try:
                    collection_info = await self.client.get_collection(
                        self.collection_name
                    )
                    existing_dims = collection_info.config.params.vectors.size

                    if existing_dims != self.embedding_dims:
                        memory_logger.warning(
                            f"Collection {self.collection_name} exists with {existing_dims} dimensions, "
                            f"but config requires {self.embedding_dims}. Recreating collection..."
                        )
                        # Delete existing collection
                        await self.client.delete_collection(self.collection_name)
                        need_create = True
                    else:
                        memory_logger.info(
                            f"Collection {self.collection_name} exists with correct dimensions ({self.embedding_dims})"
                        )
                except Exception as e:
                    memory_logger.warning(
                        f"Error checking collection info: {e}. Recreating..."
                    )
                    try:
                        await self.client.delete_collection(self.collection_name)
                    except:
                        pass  # Collection might not exist
                    need_create = True
            else:
                need_create = True

            if need_create:
                await self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.embedding_dims, distance=Distance.COSINE
                    ),
                )
                memory_logger.info(
                    f"Created Qdrant collection: {self.collection_name} with {self.embedding_dims} dimensions"
                )

        except Exception as e:
            memory_logger.error(f"Qdrant initialization failed: {e}")
            raise

    async def add_memories(self, memories: List[MemoryEntry]) -> List[str]:
        """Add memories to Qdrant."""
        try:
            points = []
            for memory in memories:
                if memory.embedding:
                    current_time = str(int(time.time()))
                    point = PointStruct(
                        id=memory.id,
                        vector=memory.embedding,
                        payload={
                            "content": memory.content,
                            "metadata": memory.metadata,
                            "created_at": memory.created_at or current_time,
                            "updated_at": memory.updated_at or current_time,
                        },
                    )
                    points.append(point)

            if points:
                await self.client.upsert(
                    collection_name=self.collection_name, points=points
                )
                return [str(point.id) for point in points]

            return []

        except Exception as e:
            memory_logger.error(f"Qdrant add memories failed: {e}")
            return []

    async def search_memories(
        self,
        query_embedding: List[float],
        user_id: str,
        limit: int,
        score_threshold: float = 0.0,
    ) -> List[MemoryEntry]:
        """Search memories in Qdrant with configurable similarity threshold filtering.

        Args:
            query_embedding: Query vector for similarity search
            user_id: User identifier to filter results
            limit: Maximum number of results to return
            score_threshold: Minimum similarity score (0.0 = no threshold, 1.0 = exact match)
        """
        try:
            # Filter by user_id
            search_filter = Filter(
                must=[
                    FieldCondition(
                        key="metadata.user_id", match=MatchValue(value=user_id)
                    )
                ]
            )

            # Apply similarity threshold if provided
            # For cosine similarity, scores range from -1 to 1, where 1 is most similar
            search_params = {
                "collection_name": self.collection_name,
                "query": query_embedding,
                "query_filter": search_filter,
                "limit": limit,
            }

            if score_threshold > 0.0:
                search_params["score_threshold"] = score_threshold
                memory_logger.debug(f"Using similarity threshold: {score_threshold}")

            response = await self.client.query_points(**search_params)

            memories = []
            for result in response.points:
                memory = MemoryEntry(
                    id=str(result.id),
                    content=result.payload.get("content", ""),
                    metadata=result.payload.get("metadata", {}),
                    # Qdrant returns similarity scores directly (higher = more similar)
                    score=result.score if result.score is not None else None,
                    created_at=result.payload.get("created_at"),
                    updated_at=result.payload.get("updated_at"),
                )
                memories.append(memory)
                # Log similarity scores for debugging
                score_str = (
                    f"{result.score:.3f}" if result.score is not None else "None"
                )
                memory_logger.debug(
                    f"Retrieved memory with score {score_str}: {result.payload.get('content', '')[:50]}..."
                )

            threshold_msg = (
                f"threshold {score_threshold}"
                if score_threshold > 0.0
                else "no threshold"
            )
            memory_logger.info(
                f"Found {len(memories)} memories with {threshold_msg} for user {user_id}"
            )
            return memories

        except Exception as e:
            memory_logger.error(f"Qdrant search failed: {e}")
            return []

    async def get_memories(self, user_id: str, limit: int) -> List[MemoryEntry]:
        """Get all memories for a user from Qdrant."""
        try:
            # Filter by user_id
            search_filter = Filter(
                must=[
                    FieldCondition(
                        key="metadata.user_id", match=MatchValue(value=user_id)
                    )
                ]
            )

            results = await self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=search_filter,
                limit=limit,
            )

            memories = []
            for point in results[0]:  # results is tuple (points, next_page_offset)
                memory = MemoryEntry(
                    id=str(point.id),
                    content=point.payload.get("content", ""),
                    metadata=point.payload.get("metadata", {}),
                    created_at=point.payload.get("created_at"),
                    updated_at=point.payload.get("updated_at"),
                )
                memories.append(memory)

            return memories

        except Exception as e:
            memory_logger.error(f"Qdrant get memories failed: {e}")
            return []

    async def delete_memory(
        self,
        memory_id: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        """Delete a specific memory from Qdrant."""
        try:
            # Convert memory_id to proper format for Qdrant
            import uuid

            try:
                # Try to parse as UUID first
                uuid.UUID(memory_id)
                point_id = memory_id
            except ValueError:
                # If not a UUID, try as integer
                try:
                    point_id = int(memory_id)
                except ValueError:
                    # If neither UUID nor integer, use it as-is and let Qdrant handle the error
                    point_id = memory_id

            await self.client.delete(
                collection_name=self.collection_name, points_selector=[point_id]
            )
            return True

        except Exception as e:
            memory_logger.error(f"Qdrant delete memory failed: {e}")
            return False

    async def delete_user_memories(self, user_id: str) -> int:
        """Delete all memories for a user from Qdrant."""
        try:
            # First count memories to delete
            memories = await self.get_memories(user_id, limit=10000)
            count = len(memories)

            if count > 0:
                # Delete by filter
                delete_filter = Filter(
                    must=[
                        FieldCondition(
                            key="metadata.user_id", match=MatchValue(value=user_id)
                        )
                    ]
                )

                await self.client.delete(
                    collection_name=self.collection_name,
                    points_selector=FilterSelector(filter=delete_filter),
                )

            return count

        except Exception as e:
            memory_logger.error(f"Qdrant delete user memories failed: {e}")
            return 0

    async def test_connection(self) -> bool:
        """Test Qdrant connection."""
        try:
            if self.client:
                await self.client.get_collections()
                return True
            return False

        except Exception as e:
            memory_logger.error(f"Qdrant connection test failed: {e}")
            return False

    async def update_memory(
        self,
        memory_id: str,
        new_content: str,
        new_embedding: List[float],
        new_metadata: Dict[str, Any],
    ) -> bool:
        """Update (upsert) an existing memory in Qdrant."""
        try:
            # Preserve original created_at from the existing point
            existing = await self.get_memory(memory_id)
            created_at = existing.created_at if existing else str(int(time.time()))

            payload = {
                "content": new_content,
                "metadata": new_metadata,
                "created_at": created_at,
                "updated_at": str(int(time.time())),
            }

            # Convert memory_id to proper format for Qdrant
            # Qdrant accepts either UUID strings or unsigned integers
            import uuid

            try:
                # Try to parse as UUID first
                uuid.UUID(memory_id)
                point_id = memory_id
            except ValueError:
                # If not a UUID, try as integer
                try:
                    point_id = int(memory_id)
                except ValueError:
                    # If neither UUID nor integer, use it as-is and let Qdrant handle the error
                    point_id = memory_id

            await self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=new_embedding,
                        payload=payload,
                    )
                ],
            )
            return True
        except Exception as e:
            memory_logger.error(f"Qdrant update memory failed: {e}")
            return False

    async def count_memories(self, user_id: str) -> int:
        """Count total number of memories for a user in Qdrant using native count API."""
        try:

            search_filter = Filter(
                must=[
                    FieldCondition(
                        key="metadata.user_id", match=MatchValue(value=user_id)
                    )
                ]
            )

            # Use Qdrant's native count API (documented in qdrant/qdrant/docs)
            # Count operation: CountPoints -> CountResponse with count result
            result = await self.client.count(
                collection_name=self.collection_name, count_filter=search_filter
            )

            return result.count

        except Exception as e:
            memory_logger.error(f"Qdrant count memories failed: {e}")
            return 0

    async def get_memory(
        self, memory_id: str, user_id: Optional[str] = None
    ) -> Optional[MemoryEntry]:
        """Get a specific memory by ID from Qdrant.

        Args:
            memory_id: Unique identifier of the memory to retrieve
            user_id: Optional user ID for validation (not used in Qdrant filtering)

        Returns:
            MemoryEntry object if found, None otherwise
        """
        try:
            # Convert memory_id to proper format for Qdrant
            import uuid

            try:
                # Try to parse as UUID first
                uuid.UUID(memory_id)
                point_id = memory_id
            except ValueError:
                # If not a UUID, try as integer
                try:
                    point_id = int(memory_id)
                except ValueError:
                    # If neither UUID nor integer, use it as-is
                    point_id = memory_id

            # Retrieve the point by ID
            points = await self.client.retrieve(
                collection_name=self.collection_name,
                ids=[point_id],
                with_payload=True,
                with_vectors=False,
            )

            if not points:
                memory_logger.debug(f"Memory not found: {memory_id}")
                return None

            point = points[0]

            # If user_id is provided, validate ownership
            if user_id:
                point_user_id = point.payload.get("metadata", {}).get("user_id")
                if point_user_id != user_id:
                    memory_logger.warning(
                        f"Memory {memory_id} does not belong to user {user_id}"
                    )
                    return None

            # Convert to MemoryEntry
            memory = MemoryEntry(
                id=str(point.id),
                content=point.payload.get("content", ""),
                metadata=point.payload.get("metadata", {}),
                created_at=point.payload.get("created_at"),
                updated_at=point.payload.get("updated_at"),
            )

            memory_logger.debug(f"Retrieved memory {memory_id}")
            return memory

        except Exception as e:
            memory_logger.error(f"Qdrant get memory failed for {memory_id}: {e}")
            return None

    async def get_memories_by_source(
        self, user_id: str, source_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        """Get all memories for a specific source (conversation) for a user.

        Filters by both user_id and source_id in metadata to return only
        memories extracted from a particular conversation.

        Args:
            user_id: User identifier
            source_id: Source/conversation identifier
            limit: Maximum number of memories to return

        Returns:
            List of MemoryEntry objects for the specified source
        """
        try:
            search_filter = Filter(
                must=[
                    FieldCondition(
                        key="metadata.user_id",
                        match=MatchValue(value=user_id),
                    ),
                    FieldCondition(
                        key="metadata.source_id",
                        match=MatchValue(value=source_id),
                    ),
                ]
            )

            results = await self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=search_filter,
                limit=limit,
            )

            memories = []
            for point in results[0]:
                memory = MemoryEntry(
                    id=str(point.id),
                    content=point.payload.get("content", ""),
                    metadata=point.payload.get("metadata", {}),
                    created_at=point.payload.get("created_at"),
                    updated_at=point.payload.get("updated_at"),
                )
                memories.append(memory)

            memory_logger.info(
                f"Found {len(memories)} memories for source {source_id} (user {user_id})"
            )
            return memories

        except Exception as e:
            memory_logger.error(f"Qdrant get memories by source failed: {e}")
            return []

    async def get_recent_memories(
        self, user_id: str, since_timestamp: int, limit: int = 100
    ) -> List[MemoryEntry]:
        """Get memories created after a given unix timestamp for a user.

        Args:
            user_id: User identifier
            since_timestamp: Unix timestamp; only memories at or after this time are returned
            limit: Maximum number of memories to return

        Returns:
            List of MemoryEntry objects
        """
        try:
            search_filter = Filter(
                must=[
                    FieldCondition(
                        key="metadata.user_id",
                        match=MatchValue(value=user_id),
                    ),
                    FieldCondition(
                        key="metadata.timestamp",
                        range=Range(gte=since_timestamp),
                    ),
                ]
            )

            results = await self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=search_filter,
                limit=limit,
            )

            memories = []
            for point in results[0]:
                memory = MemoryEntry(
                    id=str(point.id),
                    content=point.payload.get("content", ""),
                    metadata=point.payload.get("metadata", {}),
                    created_at=point.payload.get("created_at"),
                    updated_at=point.payload.get("updated_at"),
                )
                memories.append(memory)

            memory_logger.info(
                f"Found {len(memories)} recent memories since {since_timestamp} for user {user_id}"
            )
            return memories

        except Exception as e:
            memory_logger.error(f"Qdrant get recent memories failed: {e}")
            return []
