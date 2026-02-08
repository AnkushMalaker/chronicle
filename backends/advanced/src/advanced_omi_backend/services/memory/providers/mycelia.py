"""Mycelia memory service implementation.

This module provides a concrete implementation of the MemoryServiceBase interface
that uses Mycelia as the backend for all memory operations.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from advanced_omi_backend.auth import generate_jwt_for_user
from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.users import User

from ..base import MemoryEntry, MemoryServiceBase
from ..config import MemoryConfig
from ..prompts import (
    FACT_RETRIEVAL_PROMPT,
    TemporalEntity,
    get_temporal_entity_extraction_prompt,
)
from .llm_providers import _get_openai_client

memory_logger = logging.getLogger("memory_service")


def strip_markdown_json(content: str) -> str:
    """Strip markdown code block wrapper from JSON content.

    Handles formats like:
    - ```json\n{...}\n```
    - ```\n{...}\n```
    - {... } (plain JSON, returned as-is)
    """
    content = content.strip()
    if content.startswith("```"):
        # Remove opening ```json or ```
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 :]
        # Remove closing ```
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return content


class MyceliaMemoryService(MemoryServiceBase):
    """Memory service implementation using Mycelia backend.

    This class implements the MemoryServiceBase interface by delegating memory
    operations to a Mycelia server using JWT authentication from Chronicle.

    Args:
        api_url: Mycelia API endpoint URL
        timeout: Request timeout in seconds
        **kwargs: Additional configuration parameters
    """

    @property
    def provider_identifier(self) -> str:
        return "mycelia"

    def __init__(self, config: MemoryConfig):
        """Initialize Mycelia memory service.

        Args:
            config: MemoryConfig object containing mycelia_config and llm_config
        """
        super().__init__()
        self.config = config
        self.mycelia_config = config.mycelia_config or {}
        self.api_url = self.mycelia_config.get("api_url", "http://localhost:8080").rstrip("/")
        self.timeout = self.mycelia_config.get("timeout", 30)
        self._client: Optional[httpx.AsyncClient] = None

        # Store LLM config for temporal extraction
        self.llm_config = config.llm_config or {}

        memory_logger.info(f"🍄 Initializing Mycelia memory service at {self.api_url}")

    async def initialize(self) -> None:
        """Initialize Mycelia client and verify connection."""
        try:
            # Initialize HTTP client
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )

            # Test connection directly (without calling test_connection to avoid recursion)
            try:
                response = await self._client.get("/health")
                if response.status_code != 200:
                    raise RuntimeError(f"Health check failed with status {response.status_code}")
            except httpx.HTTPError as e:
                raise RuntimeError(f"Failed to connect to Mycelia service: {e}")

            self._initialized = True
            memory_logger.info("✅ Mycelia memory service initialized successfully")

        except Exception as e:
            memory_logger.error(f"❌ Failed to initialize Mycelia service: {e}")
            raise RuntimeError(f"Mycelia initialization failed: {e}")

    async def _get_user_jwt(self, user_id: str, user_email: Optional[str] = None) -> str:
        """Get JWT token for a user (with optional user lookup).

        Args:
            user_id: User ID
            user_email: Optional user email (will lookup if not provided)

        Returns:
            JWT token string

        Raises:
            ValueError: If user not found
        """
        # If email not provided, lookup user
        if not user_email:
            user = await User.get(user_id)
            if not user:
                raise ValueError(f"User {user_id} not found")
            user_email = user.email

        return generate_jwt_for_user(user_id, user_email)

    @staticmethod
    def _extract_bson_id(raw_id: Any) -> str:
        """Extract ID from Mycelia BSON format {"$oid": "..."} or plain string."""
        if isinstance(raw_id, dict) and "$oid" in raw_id:
            return raw_id["$oid"]
        return str(raw_id)

    @staticmethod
    def _extract_bson_date(date_obj: Any) -> Any:
        """Extract date from Mycelia BSON format {"$date": "..."} or plain value."""
        if isinstance(date_obj, dict) and "$date" in date_obj:
            return date_obj["$date"]
        return date_obj

    def _mycelia_object_to_memory_entry(self, obj: Dict, user_id: str) -> MemoryEntry:
        """Convert Mycelia object to MemoryEntry.

        Args:
            obj: Mycelia object from API
            user_id: User ID for metadata

        Returns:
            MemoryEntry object with full Mycelia metadata including temporal and semantic fields
        """
        memory_id = self._extract_bson_id(obj.get("_id", ""))
        memory_content = obj.get("details", "")

        # Build metadata with all Mycelia fields
        metadata = {
            "user_id": user_id,
            "name": obj.get("name", ""),
            "aliases": obj.get("aliases", []),
            "created_at": self._extract_bson_date(obj.get("createdAt")),
            "updated_at": self._extract_bson_date(obj.get("updatedAt")),
            # Semantic flags
            "isPerson": obj.get("isPerson", False),
            "isEvent": obj.get("isEvent", False),
            "isPromise": obj.get("isPromise", False),
            "isRelationship": obj.get("isRelationship", False),
        }

        # Add icon if present
        if "icon" in obj and obj["icon"]:
            metadata["icon"] = obj["icon"]

        # Add temporal information if present
        if "timeRanges" in obj and obj["timeRanges"]:
            # Convert BSON dates in timeRanges to ISO strings for JSON serialization
            time_ranges = []
            for tr in obj["timeRanges"]:
                time_range = {
                    "start": self._extract_bson_date(tr.get("start")),
                    "end": self._extract_bson_date(tr.get("end")),
                }
                if "name" in tr:
                    time_range["name"] = tr["name"]
                time_ranges.append(time_range)
            metadata["timeRanges"] = time_ranges

        return MemoryEntry(
            id=memory_id,
            content=memory_content,
            metadata=metadata,
            created_at=self._extract_bson_date(obj.get("createdAt")),
            updated_at=self._extract_bson_date(obj.get("updatedAt")),
        )

    async def _call_resource(self, action: str, jwt_token: str, **params) -> Dict[str, Any]:
        """Call Mycelia objects resource with JWT authentication.

        Args:
            action: Action to perform (create, list, get, delete, etc.)
            jwt_token: User's JWT token from Chronicle
            **params: Additional parameters for the action

        Returns:
            Response data from Mycelia

        Raises:
            RuntimeError: If API call fails
        """
        if not self._client:
            raise RuntimeError("Mycelia client not initialized")

        try:
            response = await self._client.post(
                "/api/resource/tech.mycelia.objects",
                json={"action": action, **params},
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            memory_logger.exception(
                f"Mycelia API error: {e.response.status_code} - {e.response.text}"
            )
            raise RuntimeError(f"Mycelia API error: {e.response.status_code}") from e
        except Exception as e:
            memory_logger.exception(f"Failed to call Mycelia resource: {e}")
            raise RuntimeError(f"Mycelia API call failed: {e}") from e

    async def _extract_memories_via_llm(
        self,
        transcript: str,
    ) -> List[str]:
        """Extract memories from transcript using OpenAI directly.

        Args:
            transcript: Raw transcript text

        Returns:
            List of extracted memory facts

        Raises:
            RuntimeError: If LLM call fails
        """
        try:
            # Use registry-driven default LLM with OpenAI SDK
            reg = get_models_registry()
            if not reg:
                memory_logger.warning("No registry available for LLM; cannot extract facts")
                return []
            llm_def = reg.get_default("llm")
            if not llm_def:
                memory_logger.warning("No default LLM in config.yml; cannot extract facts")
                return []
            client = _get_openai_client(
                api_key=llm_def.api_key or "", base_url=llm_def.model_url, is_async=True
            )
            response = await client.chat.completions.create(
                model=llm_def.model_name,
                messages=[
                    {"role": "system", "content": FACT_RETRIEVAL_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                response_format={"type": "json_object"},
                temperature=float(llm_def.model_params.get("temperature", 0.1)),
            )
            content = (response.choices[0].message.content or "").strip()

            if not content:
                memory_logger.warning("LLM returned empty content")
                return []

            # Parse JSON response to extract facts
            try:
                # Strip markdown wrapper if present (just in case)
                json_content = strip_markdown_json(content)
                facts_data = json.loads(json_content)
                facts = facts_data.get("facts", [])
                memory_logger.info(f"🧠 Extracted {len(facts)} facts from transcript via OpenAI")
                return facts
            except json.JSONDecodeError as e:
                memory_logger.error(f"Failed to parse LLM response as JSON: {e}")
                memory_logger.error(f"LLM response was: {content[:300]}")
                return []

        except Exception as e:
            memory_logger.error(f"Failed to extract memories via OpenAI: {e}")
            raise RuntimeError(f"OpenAI memory extraction failed: {e}") from e

    async def _extract_temporal_entity_via_llm(
        self,
        fact: str,
    ) -> Optional[TemporalEntity]:
        """Extract temporal and entity information from a fact using OpenAI directly.

        Args:
            fact: Memory fact text

        Returns:
            TemporalEntity with extracted information, or None if extraction fails
        """
        try:
            # Use registry-driven default LLM with OpenAI SDK
            reg = get_models_registry()
            if not reg:
                memory_logger.warning(
                    "No registry available for LLM; cannot extract temporal entity"
                )
                return None
            llm_def = reg.get_default("llm")
            if not llm_def:
                memory_logger.warning(
                    "No default LLM in config.yml; cannot extract temporal entity"
                )
                return None
            client = _get_openai_client(
                api_key=llm_def.api_key or "", base_url=llm_def.model_url, is_async=True
            )
            response = await client.chat.completions.create(
                model=llm_def.model_name,
                messages=[
                    {"role": "system", "content": get_temporal_entity_extraction_prompt()},
                    {
                        "role": "user",
                        "content": f"Extract temporal and entity information from this memory fact:\n\n{fact}",
                    },
                ],
                response_format={"type": "json_object"},
                temperature=float(llm_def.model_params.get("temperature", 0.1)),
            )

            content = response.choices[0].message.content

            if not content:
                memory_logger.warning("LLM returned empty content for temporal extraction")
                return None

            # Parse JSON response and validate with Pydantic
            try:
                # Strip markdown wrapper if present (just in case)
                json_content = strip_markdown_json(content)
                temporal_data = json.loads(json_content)

                # Convert timeRanges to proper format if present
                if "timeRanges" in temporal_data:
                    for time_range in temporal_data["timeRanges"]:
                        if isinstance(time_range["start"], str):
                            time_range["start"] = datetime.fromisoformat(
                                time_range["start"].replace("Z", "+00:00")
                            )
                        if isinstance(time_range["end"], str):
                            time_range["end"] = datetime.fromisoformat(
                                time_range["end"].replace("Z", "+00:00")
                            )

                temporal_entity = TemporalEntity(**temporal_data)
                memory_logger.info(
                    f"✅ Temporal extraction: isEvent={temporal_entity.isEvent}, timeRanges={len(temporal_entity.timeRanges)}, entities={temporal_entity.entities}"
                )
                return temporal_entity

            except json.JSONDecodeError as e:
                memory_logger.error(f"❌ Failed to parse temporal extraction JSON: {e}")
                memory_logger.error(f"Content (first 300 chars): {content[:300]}")
                return None
            except Exception as e:
                memory_logger.error(f"Failed to validate temporal entity: {e}")
                memory_logger.error(f"Data: {content[:300] if content else 'None'}")
                return None

        except Exception as e:
            memory_logger.error(f"Failed to extract temporal data via OpenAI: {e}")
            # Don't fail the entire memory creation if temporal extraction fails
            return None

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
        """Add memories from transcript using Mycelia.

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
        # Ensure service is initialized (lazy initialization for RQ workers)
        await self._ensure_initialized()

        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id, user_email)

            # Extract memories from transcript using OpenAI
            memory_logger.info(f"Extracting memories from transcript via OpenAI...")
            extracted_facts = await self._extract_memories_via_llm(transcript)

            if not extracted_facts:
                memory_logger.warning("No memories extracted from transcript")
                return (False, [])

            # Create Mycelia objects for each extracted fact
            memory_ids = []
            for fact in extracted_facts:
                fact_preview = fact[:50] + ("..." if len(fact) > 50 else "")

                # Extract temporal and entity information
                temporal_entity = await self._extract_temporal_entity_via_llm(fact)

                # Build object data with temporal/entity information if available
                if temporal_entity:
                    # Convert timeRanges from Pydantic models to dict format for Mycelia API
                    time_ranges = []
                    for tr in temporal_entity.timeRanges:
                        time_range_dict = {
                            "start": (
                                tr.start.isoformat() if isinstance(tr.start, datetime) else tr.start
                            ),
                            "end": tr.end.isoformat() if isinstance(tr.end, datetime) else tr.end,
                        }
                        if tr.name:
                            time_range_dict["name"] = tr.name
                        time_ranges.append(time_range_dict)

                    # Use emoji in name if available, otherwise use default
                    name_prefix = temporal_entity.emoji if temporal_entity.emoji else "Memory:"

                    object_data = {
                        "name": f"{name_prefix} {fact_preview}",
                        "details": fact,
                        "aliases": [source_id, client_id]
                        + temporal_entity.entities,  # Include extracted entities
                        "isPerson": temporal_entity.isPerson,
                        "isPromise": temporal_entity.isPromise,
                        "isEvent": temporal_entity.isEvent,
                        "isRelationship": temporal_entity.isRelationship,
                        # Note: userId is auto-injected by Mycelia from JWT
                    }

                    # Add timeRanges if temporal information was extracted
                    if time_ranges:
                        object_data["timeRanges"] = time_ranges

                    # Add emoji icon if available
                    if temporal_entity.emoji:
                        object_data["icon"] = {"text": temporal_entity.emoji}

                    memory_logger.info(
                        f"📅 Temporal extraction: isEvent={temporal_entity.isEvent}, timeRanges={len(time_ranges)}, entities={len(temporal_entity.entities)}"
                    )
                else:
                    # Fallback to basic object without temporal data
                    object_data = {
                        "name": f"Memory: {fact_preview}",
                        "details": fact,
                        "aliases": [source_id, client_id],
                        "isPerson": False,
                        "isPromise": False,
                        "isEvent": False,
                        "isRelationship": False,
                    }
                    memory_logger.warning(f"⚠️  No temporal data extracted for fact: {fact_preview}")

                result = await self._call_resource(
                    action="create", jwt_token=jwt_token, object=object_data
                )

                memory_id = result.get("insertedId")
                if memory_id:
                    memory_logger.info(
                        f"✅ Created Mycelia memory object: {memory_id} - {fact_preview}"
                    )
                    memory_ids.append(memory_id)
                else:
                    memory_logger.error(f"Failed to create memory fact: {fact}")

            if memory_ids:
                memory_logger.info(
                    f"✅ Created {len(memory_ids)} Mycelia memory objects from {len(extracted_facts)} facts"
                )
                return (True, memory_ids)
            else:
                memory_logger.error("No Mycelia memory objects were created")
                return (False, [])

        except Exception as e:
            memory_logger.error(f"Failed to add memory via Mycelia: {e}")
            return (False, [])

    async def search_memories(
        self, query: str, user_id: str, limit: int = 10, score_threshold: float = 0.0
    ) -> List[MemoryEntry]:
        """Search memories using Mycelia semantic search.

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
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # Search using Mycelia's list action with searchTerm option
            result = await self._call_resource(
                action="list",
                jwt_token=jwt_token,
                filters={},  # Auto-scoped by userId in Mycelia
                options={
                    "searchTerm": query,
                    "limit": limit,
                    "sort": {"updatedAt": -1},  # Most recent first
                },
            )

            # Convert Mycelia objects to MemoryEntry objects
            memories = []
            for i, obj in enumerate(result):
                # Calculate a simple relevance score (0-1) based on position
                # (Mycelia doesn't provide semantic similarity scores yet)
                score = 1.0 - (i * 0.1)  # Decaying score
                if score < score_threshold:
                    continue

                entry = self._mycelia_object_to_memory_entry(obj, user_id)
                entry.score = score  # Override score
                memories.append(entry)

            return memories

        except Exception as e:
            memory_logger.error(f"Failed to search memories via Mycelia: {e}")
            return []

    async def get_all_memories(self, user_id: str, limit: int = 100) -> List[MemoryEntry]:
        """Get all memories for a user from Mycelia.

        Args:
            user_id: User identifier
            limit: Maximum number of memories to return

        Returns:
            List of MemoryEntry objects for the user
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # List all objects for this user (auto-scoped by Mycelia)
            result = await self._call_resource(
                action="list",
                jwt_token=jwt_token,
                filters={},  # Auto-scoped by userId
                options={"limit": limit, "sort": {"updatedAt": -1}},  # Most recent first
            )

            # Convert Mycelia objects to MemoryEntry objects
            memories = [self._mycelia_object_to_memory_entry(obj, user_id) for obj in result]
            return memories

        except Exception as e:
            memory_logger.error(f"Failed to get memories via Mycelia: {e}")
            return []

    async def count_memories(self, user_id: str) -> Optional[int]:
        """Count memories for a user.

        Args:
            user_id: User identifier

        Returns:
            Total count of memories for the user, or None if not supported
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # Use Mycelia's mongo resource to count objects for this user
            if not self._client:
                raise RuntimeError("Mycelia client not initialized")

            response = await self._client.post(
                "/api/resource/tech.mycelia.mongo",
                json={"action": "count", "collection": "objects", "query": {"userId": user_id}},
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
            response.raise_for_status()
            return response.json()

        except Exception as e:
            memory_logger.error(f"Failed to count memories via Mycelia: {e}")
            return None

    async def get_memory(
        self, memory_id: str, user_id: Optional[str] = None
    ) -> Optional[MemoryEntry]:
        """Get a specific memory by ID from Mycelia.

        Args:
            memory_id: Unique identifier of the memory to retrieve
            user_id: Optional user identifier for authentication

        Returns:
            MemoryEntry object if found, None otherwise
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Need user ID for JWT authentication
            if not user_id:
                memory_logger.error("User ID required for Mycelia get_memory operation")
                return None

            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # Get the object by ID (auto-scoped by userId in Mycelia)
            result = await self._call_resource(action="get", jwt_token=jwt_token, id=memory_id)

            if result:
                return self._mycelia_object_to_memory_entry(result, user_id)
            else:
                memory_logger.warning(f"Memory not found with ID: {memory_id}")
                return None

        except Exception as e:
            memory_logger.error(f"Failed to get memory via Mycelia: {e}")
            return None

    async def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        """Update a specific memory's content and/or metadata in Mycelia.

        Args:
            memory_id: Unique identifier of the memory to update
            content: New content for the memory (updates 'details' field)
            metadata: New metadata to merge with existing
            user_id: Optional user ID for authentication
            user_email: Optional user email for authentication

        Returns:
            True if update succeeded, False otherwise
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Need user ID for JWT authentication
            if not user_id:
                memory_logger.error("User ID required for Mycelia update_memory operation")
                return False

            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id, user_email)

            # Build update object
            update_data: Dict[str, Any] = {}

            if content is not None:
                update_data["details"] = content

            if metadata:
                # Extract specific metadata fields that Mycelia supports
                if "name" in metadata:
                    update_data["name"] = metadata["name"]
                if "aliases" in metadata:
                    update_data["aliases"] = metadata["aliases"]
                if "isPerson" in metadata:
                    update_data["isPerson"] = metadata["isPerson"]
                if "isPromise" in metadata:
                    update_data["isPromise"] = metadata["isPromise"]
                if "isEvent" in metadata:
                    update_data["isEvent"] = metadata["isEvent"]
                if "isRelationship" in metadata:
                    update_data["isRelationship"] = metadata["isRelationship"]
                if "timeRanges" in metadata:
                    update_data["timeRanges"] = metadata["timeRanges"]
                if "icon" in metadata:
                    update_data["icon"] = metadata["icon"]

            if not update_data:
                memory_logger.warning("No update data provided")
                return False

            # Update the object (auto-scoped by userId in Mycelia)
            result = await self._call_resource(
                action="update", jwt_token=jwt_token, id=memory_id, object=update_data
            )

            updated_count = result.get("modifiedCount", 0)
            if updated_count > 0:
                memory_logger.info(f"✅ Updated Mycelia memory object: {memory_id}")
                return True
            else:
                memory_logger.warning(f"No memory updated with ID: {memory_id}")
                return False

        except Exception as e:
            memory_logger.error(f"Failed to update memory via Mycelia: {e}")
            return False

    async def delete_memory(
        self, memory_id: str, user_id: Optional[str] = None, user_email: Optional[str] = None
    ) -> bool:
        """Delete a specific memory from Mycelia.

        Args:
            memory_id: Unique identifier of the memory to delete
            user_id: Optional user identifier for authentication
            user_email: Optional user email for authentication

        Returns:
            True if successfully deleted, False otherwise
        """
        try:
            # Need user credentials for JWT - if not provided, we can't delete
            if not user_id:
                memory_logger.error("User ID required for Mycelia delete operation")
                return False

            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id, user_email)

            # Delete the object (auto-scoped by userId in Mycelia)
            result = await self._call_resource(action="delete", jwt_token=jwt_token, id=memory_id)

            deleted_count = result.get("deletedCount", 0)
            if deleted_count > 0:
                memory_logger.info(f"✅ Deleted Mycelia memory object: {memory_id}")
                return True
            else:
                memory_logger.warning(f"No memory deleted with ID: {memory_id}")
                return False

        except Exception as e:
            memory_logger.error(f"Failed to delete memory via Mycelia: {e}")
            return False

    async def delete_all_user_memories(self, user_id: str) -> int:
        """Delete all memories for a user from Mycelia.

        Args:
            user_id: User identifier

        Returns:
            Number of memories that were deleted
        """
        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # First, get all memory IDs for this user
            result = await self._call_resource(
                action="list",
                jwt_token=jwt_token,
                filters={},  # Auto-scoped by userId
                options={"limit": 10000},  # Large limit to get all
            )

            # Delete each memory individually
            deleted_count = 0
            for obj in result:
                memory_id = self._extract_bson_id(obj.get("_id", ""))
                if await self.delete_memory(memory_id, user_id):
                    deleted_count += 1

            memory_logger.info(f"✅ Deleted {deleted_count} Mycelia memories for user {user_id}")
            return deleted_count

        except Exception as e:
            memory_logger.error(f"Failed to delete user memories via Mycelia: {e}")
            return 0

    async def test_connection(self) -> bool:
        """Test connection to Mycelia service.

        Returns:
            True if connection is healthy, False otherwise
        """
        try:
            if not self._initialized:
                await self.initialize()

            if not self._client:
                return False

            # Test connection by hitting a lightweight endpoint
            response = await self._client.get("/health")
            return response.status_code == 200

        except Exception as e:
            memory_logger.error(f"Mycelia connection test failed: {e}")
            return False

    async def aclose(self) -> None:
        """Asynchronously close Mycelia client and cleanup resources."""
        memory_logger.info("Closing Mycelia memory service")
        if self._client:
            try:
                await self._client.aclose()
                memory_logger.info("✅ Mycelia HTTP client closed successfully")
            except Exception as e:
                memory_logger.error(f"Error closing Mycelia HTTP client: {e}")
        self._initialized = False

    def shutdown(self) -> None:
        """Shutdown Mycelia client and cleanup resources (sync wrapper)."""
        memory_logger.info("Shutting down Mycelia memory service")

        if self._client:
            try:
                # Try to get the current event loop
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # No running loop
                    loop = None

                if loop and loop.is_running():
                    # If we're in an async context, schedule the close operation on the running loop
                    memory_logger.info(
                        "Running event loop detected. Scheduling aclose() on the current loop."
                    )
                    try:
                        # Schedule the coroutine to run on the existing loop
                        asyncio.ensure_future(self.aclose(), loop=loop)
                        memory_logger.info("✅ Close operation scheduled on running event loop")
                    except Exception as e:
                        memory_logger.error(f"Error scheduling close on running loop: {e}")
                else:
                    # No running loop, safe to use run_until_complete
                    try:
                        asyncio.get_event_loop().run_until_complete(self.aclose())
                    except Exception as e:
                        memory_logger.error(f"Error during shutdown: {e}")
            except Exception as e:
                memory_logger.error(f"Unexpected error during shutdown: {e}")

        self._initialized = False
