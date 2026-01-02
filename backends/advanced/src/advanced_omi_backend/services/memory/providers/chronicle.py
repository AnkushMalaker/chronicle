"""Main memory service implementation.

This module provides the core MemoryService class that orchestrates
LLM providers and vector stores to provide comprehensive memory management
functionality.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, List, Optional, Tuple

from ..base import LLMProviderBase, MemoryEntry, MemoryServiceBase, VectorStoreBase
from ..config import LLMProvider as LLMProviderEnum
from ..config import MemoryConfig, VectorStoreProvider
from .llm_providers import OpenAIProvider
from .vector_stores import QdrantVectorStore

memory_logger = logging.getLogger("memory_service")


class MemoryService(MemoryServiceBase):
    """Main memory service that orchestrates LLM and vector store operations.
    
    This class implements the core memory management functionality including:
    - Memory extraction from transcripts using LLM providers
    - Semantic storage and retrieval using vector stores
    - Memory updates and deduplication
    - User-scoped memory management
    
    The service supports multiple LLM providers (OpenAI, Ollama) and vector
    stores (Qdrant), providing a flexible and extensible architecture.
    
    Attributes:
        config: Memory service configuration
        llm_provider: Active LLM provider instance
        vector_store: Active vector store instance
        _initialized: Whether the service has been initialized
    """

    def __init__(self, config: MemoryConfig):
        """Initialize the memory service with configuration.
        
        Args:
            config: MemoryConfig instance with provider settings
        """
        super().__init__()
        self.config = config
        self.llm_provider: Optional[LLMProviderBase] = None
        self.vector_store: Optional[VectorStoreBase] = None

    async def initialize(self) -> None:
        """Initialize the memory service and all its components.
        
        Sets up LLM provider and vector store based on configuration,
        tests connections, and marks the service as ready for use.
        
        Raises:
            ValueError: If unsupported provider is configured
            RuntimeError: If initialization or connection tests fail
        """
        if self._initialized:
            return

        try:
            # Initialize LLM provider
            if self.config.llm_provider in [LLMProviderEnum.OPENAI, LLMProviderEnum.OLLAMA]:
                self.llm_provider = OpenAIProvider(self.config.llm_config)
            else:
                raise ValueError(f"Unsupported LLM provider: {self.config.llm_provider}")

            # Initialize vector store
            if self.config.vector_store_provider == VectorStoreProvider.QDRANT:
                self.vector_store = QdrantVectorStore(self.config.vector_store_config)
            else:
                raise ValueError(f"Unsupported vector store provider: {self.config.vector_store_provider}")

            # Initialize vector store
            await self.vector_store.initialize()

            # Test connections
            llm_ok = await self.llm_provider.test_connection()
            vector_ok = await self.vector_store.test_connection()

            if not llm_ok:
                raise RuntimeError(
                    f"LLM provider connection failed for {self.config.llm_provider.value}. "
                    f"Check API keys, network connectivity, and service availability. "
                    f"Memory processing cannot proceed without a working LLM connection."
                )
            if not vector_ok:
                raise RuntimeError(
                    f"Vector store connection failed for {self.config.vector_store_provider.value}. "
                    f"Check that Qdrant service is running and accessible."
                )

            self._initialized = True
            memory_logger.info(
                f"✅ Memory service initialized successfully with "
                f"{self.config.llm_provider.value} + {self.config.vector_store_provider.value}"
            )

        except Exception as e:
            memory_logger.error(f"Memory service initialization failed: {e}")
            raise

    async def add_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        allow_update: bool = False,
        db_helper: Any = None
    ) -> Tuple[bool, List[str]]:
        """Add memories extracted from a transcript.
        
        Processes a transcript to extract meaningful memories using the LLM,
        generates embeddings, and stores them in the vector database. Optionally
        allows updating existing memories through LLM-driven action proposals.
        
        Args:
            transcript: Raw transcript text to extract memories from
            client_id: Client identifier for tracking
            source_id: Unique identifier for the source (audio session, chat session, etc.)
            user_id: User identifier for memory scoping
            user_email: User email address
            allow_update: Whether to allow updating existing memories
            db_helper: Optional database helper for relationship tracking
            
        Returns:
            Tuple of (success: bool, created_memory_ids: List[str])
            
        Raises:
            asyncio.TimeoutError: If processing exceeds timeout
        """
        await self._ensure_initialized()

        try:
            # Skip empty transcripts
            if not transcript or len(transcript.strip()) < 10:
                memory_logger.info(f"Skipping empty transcript for {source_id}")
                return True, []

            # Extract memories using LLM if enabled
            fact_memories_text = []
            if self.config.extraction_enabled and self.config.extraction_prompt:
                fact_memories_text = await asyncio.wait_for(
                    self.llm_provider.extract_memories(transcript, self.config.extraction_prompt),
                    timeout=self.config.timeout_seconds
                )
                memory_logger.info(f"🧠 Extracted {len(fact_memories_text)} memories from transcript for {source_id}")
            
            # Fallback to storing raw transcript if no memories extracted
            if not fact_memories_text:
                fact_memories_text = [transcript]
                memory_logger.info(f"💾 No memories extracted, storing raw transcript for {source_id}")

            memory_logger.debug(f"🧠 fact_memories_text: {fact_memories_text}")
            # Simple deduplication of extracted memories within the same call
            fact_memories_text = self._deduplicate_memories(fact_memories_text)
            memory_logger.debug(f"🧠 fact_memories_text after deduplication: {fact_memories_text}")
            # Generate embeddings
            embeddings = await asyncio.wait_for(
                self.llm_provider.generate_embeddings(fact_memories_text),
                timeout=self.config.timeout_seconds
            )
            memory_logger.info(f"embeddings generated")
            if not embeddings or len(embeddings) != len(fact_memories_text):
                error_msg = f"❌ Embedding generation failed for {source_id}: got {len(embeddings) if embeddings else 0} embeddings for {len(fact_memories_text)} memories"
                memory_logger.error(error_msg)
                raise RuntimeError(error_msg)
            
            # Create or update memory entries
            memory_entries = []
            created_ids: List[str] = []

            # If allow_update, try LLM-driven action proposal
            if allow_update and fact_memories_text:
                memory_logger.info(f"🔍 Allowing update for {source_id}")
                created_ids = await self._process_memory_updates(
                    fact_memories_text, embeddings, user_id, client_id, source_id, user_email
                )
            else:
                memory_logger.info(f"🔍 Not allowing update for {source_id}")
                # Add all extracted memories normally
                memory_entries = self._create_memory_entries(
                    fact_memories_text, embeddings, client_id, source_id, user_id, user_email
                )

            # Store new entries in vector database
            if memory_entries:
                stored_ids = await self.vector_store.add_memories(memory_entries)
                created_ids.extend(stored_ids)

            # Update database relationships if helper provided
            if created_ids and db_helper:
                await self._update_database_relationships(db_helper, source_id, created_ids)

            if created_ids:
                memory_logger.info(f"✅ Upserted {len(created_ids)} memories for {source_id}")
                return True, created_ids

            # No memories created - this is a valid outcome (duplicates, no extractable facts, etc.)
            memory_logger.info(f"ℹ️  No new memories created for {source_id}: memory_entries={len(memory_entries) if memory_entries else 0}, allow_update={allow_update}")
            return True, []

        except asyncio.TimeoutError as e:
            memory_logger.error(f"⏰ Memory processing timed out for {source_id}")
            raise e
        except Exception as e:
            memory_logger.error(f"❌ Add memory failed for {source_id}: {e}")
            raise e

    async def search_memories(self, query: str, user_id: str, limit: int = 10, score_threshold: float = 0.0) -> List[MemoryEntry]:
        """Search memories using semantic similarity.
        
        Generates an embedding for the query and searches the vector store
        for similar memories belonging to the specified user.
        
        Args:
            query: Search query text
            user_id: User identifier to filter memories
            limit: Maximum number of results to return
            score_threshold: Minimum similarity score (0.0 = no threshold)
            
        Returns:
            List of matching MemoryEntry objects ordered by relevance
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Generate query embedding
            query_embeddings = await self.llm_provider.generate_embeddings([query])
            if not query_embeddings or not query_embeddings[0]:
                memory_logger.error("Failed to generate query embedding")
                return []

            # Search in vector store
            results = await self.vector_store.search_memories(
                query_embeddings[0], user_id, limit, score_threshold
            )

            memory_logger.info(f"🔍 Found {len(results)} memories for query '{query}' (user: {user_id})")
            return results

        except Exception as e:
            memory_logger.error(f"Search memories failed: {e}")
            return []

    async def get_all_memories(self, user_id: str, limit: int = 100) -> List[MemoryEntry]:
        """Get all memories for a specific user.
        
        Retrieves all stored memories for the given user without
        similarity filtering.
        
        Args:
            user_id: User identifier
            limit: Maximum number of memories to return
            
        Returns:
            List of MemoryEntry objects for the user
        """
        if not self._initialized:
            await self.initialize()

        try:
            memories = await self.vector_store.get_memories(user_id, limit)
            memory_logger.info(f"📚 Retrieved {len(memories)} memories for user {user_id}")
            return memories
        except Exception as e:
            memory_logger.error(f"Get all memories failed: {e}")
            return []

    async def count_memories(self, user_id: str) -> Optional[int]:
        """Count total number of memories for a user.
        
        Uses the vector store's native count capabilities.
        
        Args:
            user_id: User identifier
            
        Returns:
            Total count of memories for the user, or None if not supported
        """
        if not self._initialized:
            await self.initialize()

        try:
            count = await self.vector_store.count_memories(user_id)
            memory_logger.info(f"🔢 Total {count} memories for user {user_id}")
            return count
        except Exception as e:
            memory_logger.error(f"Count memories failed: {e}")
            return None

    async def delete_memory(self, memory_id: str, user_id: Optional[str] = None, user_email: Optional[str] = None) -> bool:
        """Delete a specific memory by ID.
        
        Args:
            memory_id: Unique identifier of the memory to delete
            
        Returns:
            True if successfully deleted, False otherwise
        """
        if not self._initialized:
            await self.initialize()

        try:
            success = await self.vector_store.delete_memory(memory_id)
            if success:
                memory_logger.info(f"🗑️ Deleted memory {memory_id}")
            return success
        except Exception as e:
            memory_logger.error(f"Delete memory failed: {e}")
            return False

    async def delete_all_user_memories(self, user_id: str) -> int:
        """Delete all memories for a specific user.
        
        Args:
            user_id: User identifier
            
        Returns:
            Number of memories that were deleted
        """
        if not self._initialized:
            await self.initialize()

        try:
            count = await self.vector_store.delete_user_memories(user_id)
            memory_logger.info(f"🗑️ Deleted {count} memories for user {user_id}")
            return count
        except Exception as e:
            memory_logger.error(f"Delete user memories failed: {e}")
            return 0

    async def test_connection(self) -> bool:
        """Test if the memory service and its dependencies are working.
        
        Returns:
            True if all connections are healthy, False otherwise
        """
        try:
            if not self._initialized:
                await self.initialize()
            return True
        except Exception as e:
            memory_logger.error(f"Connection test failed: {e}")
            return False

    def shutdown(self) -> None:
        """Shutdown the memory service and clean up resources."""
        self._initialized = False
        self.llm_provider = None
        self.vector_store = None
        memory_logger.info("Memory service shut down")

    # Private helper methods

    def _deduplicate_memories(self, memories_text: List[str]) -> List[str]:
        """Remove near-duplicate memories from the same extraction session.
        
        Args:
            memories_text: List of extracted memory strings
            
        Returns:
            Deduplicated list of memory strings
        """
        def _collapse_text_for_dedup(text: str) -> str:
            """Normalize text for deduplication by removing common words and punctuation."""
            t = text.lower()
            # Remove common filler words to collapse near-duplicates
            stop = {"my", "is", "the", "a", "an", "are", "to", "of", "and"}
            # Remove basic punctuation
            for ch in [",", ".", "!", "?", ":", ";"]:
                t = t.replace(ch, " ")
            tokens = [tok for tok in t.split() if tok not in stop]
            return " ".join(tokens)

        seen_collapsed = set()
        deduped_text: List[str] = []
        
        for memory_text in memories_text:
            key = _collapse_text_for_dedup(memory_text)
            if key not in seen_collapsed:
                seen_collapsed.add(key)
                deduped_text.append(memory_text)
                
        if len(deduped_text) != len(memories_text):
            memory_logger.info(f"🧹 Deduplicated memories: {len(memories_text)} -> {len(deduped_text)}")
            
        return deduped_text

    def _create_memory_entries(
        self,
        fact_memories_text: List[str],
        embeddings: List[List[float]],
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str
    ) -> List[MemoryEntry]:
        """Create MemoryEntry objects from extracted memories.
        
        Args:
            fact_memories_text: List of factmemory content strings
            embeddings: Corresponding embedding vectors
            client_id: Client identifier
            source_id: Source session identifier
            user_id: User identifier
            user_email: User email
            
        Returns:
            List of MemoryEntry objects ready for storage
        """
        memory_entries = []
        
        for memory_text, embedding in zip(fact_memories_text, embeddings):
            memory_id = str(uuid.uuid4())
            memory_entries.append(
                MemoryEntry(
                    id=memory_id,
                    content=memory_text,
                    metadata={
                        "source": "offline_streaming",
                        "client_id": client_id,
                        "source_id": source_id,
                        "user_id": user_id,
                        "user_email": user_email,
                        "timestamp": int(time.time()),
                        "extraction_enabled": self.config.extraction_enabled,
                    },
                    embedding=embedding,
                    created_at=str(int(time.time())),
                )
            )
        
        return memory_entries

    async def _process_memory_updates(
        self,
        memories_text: List[str],
        embeddings: List[List[float]],
        user_id: str,
        client_id: str,
        source_id: str,
        user_email: str
    ) -> List[str]:
        """Process memory updates using LLM-driven action proposals.
        
        This method implements the intelligent memory (can be fact or summarized facts) updating logic
        that decides whether to add, update, or delete memories based
        on existing context and new information.
        
        Args:
            memories_text: List of new memory content
            embeddings: Corresponding embeddings
            user_id: User identifier
            client_id: Client identifier
            source_id: Source session identifier
            user_email: User email
            
        Returns:
            List of created/updated memory IDs
        """
        created_ids: List[str] = []
        
        # For each new fact, find top-5 existing memories as retrieval set
        retrieved_old_memory = []
        new_message_embeddings = {}
        
        for new_mem, emb in zip(memories_text, embeddings):
            new_message_embeddings[new_mem] = emb
            try:
                candidates = await self.vector_store.search_memories(
                    query_embedding=emb,
                    user_id=user_id,
                    limit=5,
                )
                for mem in candidates:
                    retrieved_old_memory.append({"id": mem.id, "text": mem.content})
            except Exception as e_search:
                memory_logger.warning(f"Search failed while preparing updates: {e_search}")

        # Dedupe by id and prepare temp mapping
        uniq = {}
        for item in retrieved_old_memory:
            uniq[item["id"]] = item
        retrieved_old_memory = list(uniq.values())

        # Map to temp IDs to avoid hallucinations
        temp_uuid_mapping = {}
        for idx, item in enumerate(retrieved_old_memory):
            temp_uuid_mapping[str(idx)] = item["id"]
            retrieved_old_memory[idx]["id"] = str(idx)

        # Ask LLM for actions
        try:
            memory_logger.info(
                f"🔍 Asking LLM for actions with {len(retrieved_old_memory)} old memories "
                f"and {len(memories_text)} new facts"
            )
            memory_logger.debug(f"🧠 Individual facts being sent to LLM: {memories_text}")
            
            # add update or delete etc actions using DEFAULT_UPDATE_MEMORY_PROMPT
            actions_obj = await self.llm_provider.propose_memory_actions(
                retrieved_old_memory=retrieved_old_memory,
                new_facts=memories_text,
                custom_prompt=None,
            )
            memory_logger.info(f"📝 UpdateMemory LLM returned: {type(actions_obj)} - {actions_obj}")
        except Exception as e_actions:
            memory_logger.error(f"LLM propose_memory_actions failed: {e_actions}")
            actions_obj = {}

        # Process the proposed actions
        actions_list = self._normalize_actions(actions_obj)
        created_ids = await self._apply_memory_actions(
            actions_list, new_message_embeddings, temp_uuid_mapping,
            client_id, source_id, user_id, user_email
        )

        return created_ids

    def _normalize_actions(self, actions_obj: Any) -> List[dict]:
        """Normalize LLM response into a list of action dictionaries.
        
        Args:
            actions_obj: Raw LLM response object
            
        Returns:
            List of normalized action dictionaries
        """
        actions_list = []
        
        try:
            memory_logger.debug(f"Normalizing actions from: {actions_obj}")
            if isinstance(actions_obj, dict):
                memory_field = actions_obj.get("memory")
                if isinstance(memory_field, list):
                    actions_list = memory_field
                elif isinstance(actions_obj.get("facts"), list):
                    actions_list = [{"event": "ADD", "text": str(t)} for t in actions_obj["facts"]]
                else:
                    # Pick first list field found
                    for v in actions_obj.values():
                        if isinstance(v, list):
                            actions_list = v
                            break
            elif isinstance(actions_obj, list):
                actions_list = actions_obj
                
            memory_logger.info(f"📋 Normalized to {len(actions_list)} actions: {actions_list}")
        except Exception as normalize_err:
            memory_logger.warning(f"Failed to normalize actions: {normalize_err}")
            actions_list = []
            
        return actions_list

    async def _apply_memory_actions(
        self,
        actions_list: List[dict],
        new_message_embeddings: dict,
        temp_uuid_mapping: dict,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str
    ) -> List[str]:
        """Apply the proposed memory actions.
        
        Args:
            actions_list: List of action dictionaries
            new_message_embeddings: Pre-computed embeddings for new content
            temp_uuid_mapping: Mapping from temporary IDs to real IDs
            client_id: Client identifier
            source_id: Source session identifier
            user_id: User identifier
            user_email: User email
            
        Returns:
            List of created/updated memory IDs
        """
        created_ids: List[str] = []
        memory_entries = []
        
        memory_logger.info(f"⚡ Processing {len(actions_list)} actions")
        
        for resp in actions_list:
            # Allow plain string entries → ADD action
            if isinstance(resp, str):
                resp = {"event": "ADD", "text": resp}
            if not isinstance(resp, dict):
                continue

            event_type = resp.get("event", "ADD")
            action_text = resp.get("text") or resp.get("memory")
            
            if not action_text or not isinstance(action_text, str):
                memory_logger.warning(f"Skipping action with no text: {resp}")
                continue

            memory_logger.debug(f"Processing action: {event_type} - {action_text[:50]}...")

            base_metadata = {
                "source": "offline_streaming",
                "client_id": client_id,
                "source_id": source_id,
                "user_id": user_id,
                "user_email": user_email,
                "timestamp": int(time.time()),
                "extraction_enabled": self.config.extraction_enabled,
            }

            # Get embedding (use precomputed if available, otherwise generate)
            emb = new_message_embeddings.get(action_text)
            if emb is None:
                try:
                    gen = await asyncio.wait_for(
                        self.llm_provider.generate_embeddings([action_text]),
                        timeout=self.config.timeout_seconds,
                    )
                    emb = gen[0] if gen else None
                except Exception as gen_err:
                    memory_logger.warning(f"Embedding generation failed for action text: {gen_err}")
                    emb = None

            if event_type == "ADD":
                if emb is None:
                    memory_logger.warning(f"Skipping ADD action due to missing embedding: {action_text}")
                    continue
                    
                memory_id = str(uuid.uuid4())
                memory_entries.append(
                    MemoryEntry(
                        id=memory_id,
                        content=action_text,
                        metadata=base_metadata,
                        embedding=emb,
                        created_at=str(int(time.time())),
                    )
                )
                memory_logger.info(f"➕ Added new memory: {memory_id} - {action_text[:50]}...")
                
            elif event_type == "UPDATE":
                provided_id = resp.get("id")
                actual_id = temp_uuid_mapping.get(str(provided_id), provided_id)
                
                if actual_id and emb is not None:
                    try:
                        updated = await self.vector_store.update_memory(
                            memory_id=str(actual_id),
                            new_content=action_text,
                            new_embedding=emb,
                            new_metadata=base_metadata,
                        )
                        if updated:
                            created_ids.append(str(actual_id))
                            memory_logger.info(f"🔄 Updated memory: {actual_id} - {action_text[:50]}...")
                        else:
                            memory_logger.warning(f"Failed to update memory {actual_id}")
                    except Exception as update_err:
                        memory_logger.error(f"Update memory failed: {update_err}")
                else:
                    memory_logger.warning(f"Skipping UPDATE due to missing ID or embedding")
                    
            elif event_type == "DELETE":
                provided_id = resp.get("id")
                actual_id = temp_uuid_mapping.get(str(provided_id), provided_id)
                if actual_id:
                    try:
                        deleted = await self.vector_store.delete_memory(str(actual_id))
                        if deleted:
                            memory_logger.info(f"🗑️ Deleted memory {actual_id}")
                        else:
                            memory_logger.warning(f"Failed to delete memory {actual_id}")
                    except Exception as delete_err:
                        memory_logger.error(f"Delete memory failed: {delete_err}")
                else:
                    memory_logger.warning(f"Skipping DELETE due to missing ID: {provided_id}")
                    
            elif event_type == "NONE":
                memory_logger.debug(f"NONE action - no changes for: {action_text[:50]}...")
                continue
            else:
                memory_logger.warning(f"Unknown event type: {event_type}")
        
        # Store new entries
        if memory_entries:
            stored_ids = await self.vector_store.add_memories(memory_entries)
            created_ids.extend(stored_ids)

        memory_logger.info(f"✅ Actions processed: {len(memory_entries)} new entries, {len(created_ids)} total changes")
        return created_ids

    async def _update_database_relationships(self, db_helper: Any, source_id: str, created_ids: List[str]) -> None:
        """Update database relationships for created memories.
        
        Args:
            db_helper: Database helper instance
            source_id: Source session identifier
            created_ids: List of created memory IDs
        """
        for memory_id in created_ids:
            try:
                await db_helper.add_memory_reference(source_id, memory_id, "created")
            except Exception as db_error:
                memory_logger.error(f"Database relationship update failed: {db_error}")


# Example usage function
async def example_usage():
    """Example of how to use the memory service."""
    from .config import build_memory_config_from_env

    # Build config from environment
    config = build_memory_config_from_env()
    
    # Initialize service
    memory_service = MemoryService(config)
    await memory_service.initialize()
    
    # Add memory
    success, memory_ids = await memory_service.add_memory(
        transcript="User discussed their goals for the next quarter.",
        client_id="client123",
        source_id="audio456",
        user_id="user789",
        user_email="user@example.com"
    )
    
    if success:
        print(f"✅ Added memories: {memory_ids}")
        
        # Search memories
        results = await memory_service.search_memories(
            query="quarterly goals",
            user_id="user789",
            limit=5
        )
        print(f"🔍 Found {len(results)} search results")
        
        # Get all memories
        all_memories = await memory_service.get_all_memories(
            user_id="user789",
            limit=100
        )
        print(f"📚 Total memories: {len(all_memories)}")
        
        # Clean up test data
        for memory_id in memory_ids:
            await memory_service.delete_memory(memory_id)
        print("🧹 Cleaned up test data")
    
    memory_service.shutdown()


if __name__ == "__main__":
    import asyncio
    asyncio.run(example_usage())