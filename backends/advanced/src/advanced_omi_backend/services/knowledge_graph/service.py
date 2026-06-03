"""Knowledge Graph Service for entity and relationship management.

This module provides the main service for:
- Extracting entities and relationships from conversations
- Storing and retrieving entities from FalkorDB
- Querying the knowledge graph

Each user's data lives in its own per-user FalkorDB graph (the same
``chronicle_<user_id>`` graph used by chronicle's MemoryService and
ObsidianService — so chunk→entity BFS in chronicle resolves naturally
within a single graph). The graph itself is the user-isolation boundary;
queries do not filter by ``user_id``.
"""

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..graph_client import (
    GraphClient,
    GraphReadInterface,
    GraphWriteInterface,
    graph_name_for_user,
)
from . import queries
from .entity_extractor import extract_entities_from_transcript, parse_natural_datetime
from .kb import KnowledgeBaseManager
from .models import Entity, EntityType, ExtractionResult, Relationship, RelationshipType

logger = logging.getLogger("knowledge_graph")

# Global service instance
_knowledge_graph_service: Optional["KnowledgeGraphService"] = None
_service_lock = threading.Lock()


class KnowledgeGraphService:
    """Service for managing knowledge graph entities and relationships.

    This service handles:
    - Entity extraction from conversation transcripts
    - CRUD operations on entities and relationships
    - Graph queries (timeline, search, related entities)
    - Conversation document browsing (ConvDoc/ConvEntity from chronicle memory)
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ):
        """Initialize the knowledge graph service.

        Args:
            host: FalkorDB host (defaults to FALKORDB_HOST env var)
            port: FalkorDB port (defaults to FALKORDB_PORT env var)
        """
        self.host = host or os.getenv("FALKORDB_HOST", "falkordb")
        self.port = port or int(os.getenv("FALKORDB_PORT", "6379"))

        # Per-user graph cache: user_id -> (client, read, write).
        # Uses the same graph name as chronicle's MemoryService
        # (``chronicle_<user_id>``) so chunk→entity BFS in chronicle resolves
        # within a single graph.
        self._io_cache: Dict[
            str, Tuple[GraphClient, GraphReadInterface, GraphWriteInterface]
        ] = {}
        self._io_lock = threading.Lock()
        self._kb = KnowledgeBaseManager()

    def _get_io(
        self, user_id: str
    ) -> Tuple[GraphClient, GraphReadInterface, GraphWriteInterface]:
        """Return ``(client, read, write)`` for ``user_id``'s per-user graph."""
        cached = self._io_cache.get(user_id)
        if cached is not None:
            return cached

        with self._io_lock:
            cached = self._io_cache.get(user_id)
            if cached is not None:
                return cached

            client = GraphClient(
                host=self.host,
                port=self.port,
                graph_name=graph_name_for_user(user_id),
            )
            read = GraphReadInterface(client)
            write = GraphWriteInterface(client)
            io = (client, read, write)
            self._io_cache[user_id] = io
            return io

    # =========================================================================
    # CONVERSATION PROCESSING
    # =========================================================================

    async def process_conversation(
        self,
        conversation_id: str,
        transcript: str,
        user_id: str,
        conversation_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process a conversation to extract and store entities.

        This is the main entry point called from memory jobs after
        memory extraction completes.

        Args:
            conversation_id: Unique ID of the conversation
            transcript: Full conversation transcript
            user_id: User who owns the conversation
            conversation_name: Optional display name for the conversation

        Returns:
            Dictionary with extraction and storage results
        """
        if not transcript or not transcript.strip():
            logger.debug(f"Empty transcript for conversation {conversation_id}")
            return {"entities": 0, "relationships": 0}

        t_start = time.perf_counter()
        t0 = time.perf_counter()
        extraction = await extract_entities_from_transcript(
            transcript=transcript,
            conversation_id=conversation_id,
        )
        t_ent_llm = time.perf_counter() - t0

        result = await self.store_extraction(
            extraction=extraction,
            conversation_id=conversation_id,
            user_id=user_id,
            conversation_name=conversation_name,
        )
        result["timings"] = {
            "ent_llm": t_ent_llm,
            "total": time.perf_counter() - t_start,
        }
        return result

    async def store_extraction(
        self,
        extraction: ExtractionResult,
        conversation_id: str,
        user_id: str,
        conversation_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist an already-extracted ExtractionResult for ``user_id``.

        Split out from ``process_conversation`` so callers can run the
        entity-extraction LLM in parallel with chronicle's add_memory
        (doc-gen LLM + chunk write) and only enter this storage phase
        once chunks are guaranteed to be in the per-user graph — that
        ordering is required for ``_link_chunks_to_entities`` to
        actually resolve to the chunk nodes.
        """
        t_start = time.perf_counter()
        try:
            if not extraction.entities:
                logger.info(
                    "store_extraction %s: 0 entities  total=%.2fs",
                    conversation_id,
                    time.perf_counter() - t_start,
                )
                return {"entities": 0, "relationships": 0}

            # Create conversation entity node
            await self._create_conversation_entity(
                conversation_id=conversation_id,
                user_id=user_id,
                name=conversation_name or f"Conversation {conversation_id[:8]}",
            )

            # Store extracted entities
            entity_id_map = await self._store_entities(
                extraction=extraction,
                user_id=user_id,
                conversation_id=conversation_id,
            )

            # Store relationships
            rel_count = await self._store_relationships(
                extraction=extraction,
                user_id=user_id,
                entity_id_map=entity_id_map,
                conversation_id=conversation_id,
            )

            # Link entities to conversation
            await self._link_entities_to_conversation(
                entity_ids=list(entity_id_map.values()),
                conversation_id=conversation_id,
                user_id=user_id,
            )

            # Link chunks to entities so chronicle's BFS expansion at search
            # time can walk from a chunk hit through shared entities to other
            # chunks. Conversation-coarse (every chunk → every entity in this
            # conversation); the caller must guarantee chunks for this
            # conversation_id are already in the per-user graph before this
            # method runs (see chat_service for the parallel-LLM ordering).
            self._link_chunks_to_entities(
                entity_ids=list(entity_id_map.values()),
                conversation_id=conversation_id,
                user_id=user_id,
            )

            t_total = time.perf_counter() - t_start
            logger.info(
                "store_extraction %s: entities=%d rels=%d  store=%.2fs",
                conversation_id,
                len(entity_id_map),
                rel_count,
                t_total,
            )
            return {
                "entities": len(entity_id_map),
                "relationships": rel_count,
                "entity_ids": list(entity_id_map.values()),
            }
        except Exception as e:
            logger.error(f"Error storing extraction for {conversation_id}: {e}")
            return {"entities": 0, "relationships": 0, "error": str(e)}

    async def _create_conversation_entity(
        self,
        conversation_id: str,
        user_id: str,
        name: str,
    ) -> str:
        """Create or update a conversation entity node."""
        _, _, write = self._get_io(user_id)
        entity_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        params = {
            "id": entity_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "name": name,
            "details": None,
            "metadata": "{}",
            "created_at": now,
            "updated_at": now,
        }

        write.run(queries.CREATE_CONVERSATION_ENTITY, **params)
        return entity_id

    async def _store_entities(
        self,
        extraction: ExtractionResult,
        user_id: str,
        conversation_id: str,
    ) -> Dict[str, str]:
        """Store extracted entities in FalkorDB.

        Returns:
            Mapping of entity name (lowercase) to entity ID
        """
        _, _, write = self._get_io(user_id)
        entity_id_map: Dict[str, str] = {}
        now = datetime.now(timezone.utc).isoformat()

        for extracted in extraction.entities:
            # Check if entity already exists in this user's graph
            existing = self._find_entity_by_name(extracted.name, user_id)
            if existing:
                entity_id_map[extracted.name.lower()] = existing["id"]
                continue

            entity_id = str(uuid.uuid4())

            # Parse event times if present
            start_time = None
            end_time = None
            if extracted.type == "event" and extracted.when:
                start_time = parse_natural_datetime(extracted.when)
                if start_time:
                    start_time = start_time.isoformat()

            params = {
                "id": entity_id,
                "name": extracted.name,
                "type": extracted.type,
                "user_id": user_id,
                "details": extracted.details,
                "icon": extracted.icon,
                "metadata": "{}",
                "created_at": now,
                "updated_at": now,
                "location": None,
                "start_time": start_time,
                "end_time": end_time,
                "conversation_id": None,
            }

            write.run(queries.CREATE_ENTITY_SIMPLE, **params)
            entity_id_map[extracted.name.lower()] = entity_id
            extraction.stored_entity_ids.append(entity_id)

        return entity_id_map

    async def _store_relationships(
        self,
        extraction: ExtractionResult,
        user_id: str,
        entity_id_map: Dict[str, str],
        conversation_id: str,
    ) -> int:
        """Store extracted relationships in FalkorDB."""
        _, _, write = self._get_io(user_id)
        count = 0
        now = datetime.now(timezone.utc).isoformat()

        for rel in extraction.relationships:
            # Handle "speaker" as a special case - could be linked to user profile
            source_name = rel.subject.lower()
            target_name = rel.object.lower()

            # Skip if we don't have both entities
            if source_name not in entity_id_map and source_name != "speaker":
                continue
            if target_name not in entity_id_map:
                continue

            # For "speaker", we could create a user entity or skip
            if source_name == "speaker":
                # For now, skip speaker relationships - could be enhanced later
                continue

            rel_id = str(uuid.uuid4())

            params = {
                "id": rel_id,
                "source_id": entity_id_map[source_name],
                "target_id": entity_id_map[target_name],
                "type": rel.relation.upper(),
                "user_id": user_id,
                "context": None,
                "timestamp": now,
                "start_date": None,
                "end_date": None,
                "metadata": "{}",
                "created_at": now,
            }

            write.run(queries.CREATE_RELATIONSHIP, **params)
            extraction.stored_relationship_ids.append(rel_id)
            count += 1

        return count

    async def _link_entities_to_conversation(
        self,
        entity_ids: List[str],
        conversation_id: str,
        user_id: str,
    ) -> None:
        """Link entities to their source conversation."""
        _, _, write = self._get_io(user_id)
        now = datetime.now(timezone.utc).isoformat()

        for entity_id in entity_ids:
            params = {
                "entity_id": entity_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "rel_id": str(uuid.uuid4()),
                "timestamp": now,
                "context": None,
            }
            write.run(queries.LINK_ENTITY_TO_CONVERSATION, **params)

    def _link_chunks_to_entities(
        self,
        entity_ids: List[str],
        conversation_id: str,
        user_id: str,
    ) -> None:
        """Link each ConvChunk in this conversation to each extracted Entity.

        Non-fatal: KG augmentation is opportunistic, so any failure (e.g.
        chunks not yet written by the chronicle provider) logs but does not
        propagate. Mirrors the non-fatal handling in chat_service.
        """
        if not entity_ids:
            return
        try:
            _, _, write = self._get_io(user_id)
            write.run(
                queries.LINK_CHUNKS_TO_ENTITIES,
                entity_ids=entity_ids,
                conversation_id=conversation_id,
            )
        except Exception as e:
            logger.warning(
                f"Chunk→Entity linking failed for conversation {conversation_id}: {e}"
            )

    def _find_entity_by_name(self, name: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Find existing entity by name in this user's graph."""
        _, read, _ = self._get_io(user_id)
        results = read.run(queries.FIND_ENTITY_BY_NAME, name=name)
        if results:
            return dict(results[0]["e"])
        return None

    # =========================================================================
    # ENTITY CRUD
    # =========================================================================

    async def get_entities(
        self,
        user_id: str,
        entity_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Entity]:
        """Get entities for a user, optionally filtered by type.

        Args:
            user_id: User ID to filter by
            entity_type: Optional entity type filter
            limit: Maximum number of entities to return

        Returns:
            List of Entity objects
        """
        _, read, _ = self._get_io(user_id)

        results = read.run(
            queries.GET_ENTITIES_BY_USER,
            type=entity_type,
            limit=limit,
        )

        entities = []
        for row in results:
            entity_data = dict(row["e"])
            entity_data["relationship_count"] = row.get("relationship_count", 0)
            entities.append(self._row_to_entity(entity_data))

        return entities

    async def get_entity(
        self,
        entity_id: str,
        user_id: str,
    ) -> Optional[Entity]:
        """Get a single entity by ID.

        Args:
            entity_id: Entity UUID
            user_id: User ID for permission check

        Returns:
            Entity object or None if not found
        """
        _, read, _ = self._get_io(user_id)

        results = read.run(queries.GET_ENTITY_BY_ID, id=entity_id)

        if not results:
            return None

        entity_data = dict(results[0]["e"])
        entity_data["relationship_count"] = results[0].get("relationship_count", 0)
        return self._row_to_entity(entity_data)

    async def get_entity_relationships(
        self,
        entity_id: str,
        user_id: str,
    ) -> List[Relationship]:
        """Get all relationships for an entity.

        Args:
            entity_id: Entity UUID
            user_id: User ID for permission check

        Returns:
            List of Relationship objects
        """
        _, read, _ = self._get_io(user_id)

        results = read.run(queries.GET_ENTITY_RELATIONSHIPS, entity_id=entity_id)

        relationships = []
        if not results:
            return relationships

        row = results[0]

        # Process outgoing relationships
        for item in row.get("outgoing", []):
            if item.get("rel") and item.get("target"):
                rel_data = dict(item["rel"])
                rel_data["source_id"] = entity_id
                rel_data["target_id"] = item["target"]["id"]
                rel_data["target_entity"] = self._row_to_entity(dict(item["target"]))
                relationships.append(self._row_to_relationship(rel_data))

        # Process incoming relationships
        for item in row.get("incoming", []):
            if item.get("rel") and item.get("source"):
                rel_data = dict(item["rel"])
                rel_data["source_id"] = item["source"]["id"]
                rel_data["target_id"] = entity_id
                rel_data["source_entity"] = self._row_to_entity(dict(item["source"]))
                relationships.append(self._row_to_relationship(rel_data))

        return relationships

    async def search_entities(
        self,
        query: str,
        user_id: str,
        limit: int = 20,
    ) -> List[Entity]:
        """Search entities by name or details.

        Args:
            query: Search query string
            user_id: User ID to filter by
            limit: Maximum results to return

        Returns:
            List of matching Entity objects
        """
        _, read, _ = self._get_io(user_id)

        results = read.run(
            queries.SEARCH_ENTITIES_BY_NAME,
            query=query,
            limit=limit,
        )

        entities = []
        for row in results:
            entity_data = dict(row["e"])
            entity_data["relationship_count"] = row.get("relationship_count", 0)
            entities.append(self._row_to_entity(entity_data))

        return entities

    async def update_entity(
        self,
        entity_id: str,
        user_id: str,
        name: Optional[str] = None,
        details: Optional[str] = None,
        icon: Optional[str] = None,
    ) -> Optional[Entity]:
        """Update an entity's fields (partial update via COALESCE).

        Args:
            entity_id: Entity UUID
            user_id: User ID for permission check
            name: New name (None keeps existing)
            details: New details (None keeps existing)
            icon: New icon (None keeps existing)

        Returns:
            Updated Entity object or None if not found
        """
        _, _, write = self._get_io(user_id)

        results = write.run(
            queries.UPDATE_ENTITY,
            id=entity_id,
            name=name,
            details=details,
            icon=icon,
            metadata=None,
            now=datetime.now(timezone.utc).isoformat(),
        )

        if not results:
            return None

        entity_data = dict(results[0]["e"])
        return self._row_to_entity(entity_data)

    async def delete_entity(
        self,
        entity_id: str,
        user_id: str,
    ) -> bool:
        """Delete an entity and its relationships.

        Args:
            entity_id: Entity UUID to delete
            user_id: User ID for permission check

        Returns:
            True if deleted, False if not found
        """
        _, _, write = self._get_io(user_id)

        results = write.run(queries.DELETE_ENTITY, id=entity_id)

        deleted = results[0]["deleted_count"] if results else 0
        return deleted > 0

    async def delete_all_user_entities(self, user_id: str) -> int:
        """Delete every :Entity (and :Conversation, which carries :Entity) for a user.

        DETACH DELETE removes incident :RELATED_TO and :MENTIONED_IN edges with
        the nodes. Note: this is a partial wipe within the per-user graph
        (Entity nodes only). For a full per-user wipe (including ConvDoc/
        ConvChunk/ConvEntity from chronicle's MemoryService), use
        ``MemoryService.delete_all_user_memories`` which drops the whole graph.

        If the per-user graph does not yet exist (FalkorDB raises "Invalid
        graph operation on empty key" on MATCH against an empty Redis key)
        we treat it as a no-op and return 0.
        """
        _, _, write = self._get_io(user_id)
        try:
            results = write.run(queries.DELETE_USER_ENTITIES)
        except Exception as exc:
            logger.debug(
                "delete_all_user_entities for %s skipped (likely empty graph): %s",
                user_id,
                exc,
            )
            return 0
        return results[0]["deleted_count"] if results else 0

    # =========================================================================
    # CONVERSATION DOC BROWSING (ConvDoc / ConvEntity from chronicle memory)
    # =========================================================================

    async def get_conversation_docs(
        self,
        user_id: str,
        person: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get conversation documents with linked people.

        Args:
            user_id: User ID to filter by
            person: Optional person name filter
            limit: Maximum results to return

        Returns:
            List of conversation doc dicts with people arrays
        """
        _, read, _ = self._get_io(user_id)

        if person:
            results = read.run(
                queries.GET_CONVERSATION_DOCS_BY_PERSON,
                person=person,
                limit=limit,
            )
        else:
            results = read.run(queries.GET_CONVERSATION_DOCS, limit=limit)

        docs = []
        for row in results:
            # Filter out null entries from collect()
            people = [
                p for p in (row.get("people") or []) if p is not None and p.get("name")
            ]
            # Deduplicate people by name
            seen_names = set()
            unique_people = []
            for p in people:
                if p["name"] not in seen_names:
                    seen_names.add(p["name"])
                    unique_people.append(p)

            docs.append(
                {
                    "conversation_id": row.get("conversation_id"),
                    "title": row.get("title"),
                    "summary": row.get("summary"),
                    "date": row.get("date"),
                    "updated_at": row.get("updated_at"),
                    "people": unique_people,
                }
            )

        return docs

    async def get_people(
        self,
        user_id: str,
    ) -> List[Dict[str, Any]]:
        """Get distinct people (ConvEntity) with mention counts.

        Args:
            user_id: User ID to filter by

        Returns:
            List of people dicts with name, description, mention_count
        """
        _, read, _ = self._get_io(user_id)

        results = read.run(queries.GET_PEOPLE)

        people = []
        for row in results:
            people.append(
                {
                    "name": row.get("name"),
                    "description": row.get("description"),
                    "mention_count": row.get("mention_count", 0),
                }
            )

        return people

    # =========================================================================
    # TIMELINE
    # =========================================================================

    async def get_timeline(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        limit: int = 100,
    ) -> List[Entity]:
        """Get entities within a time range.

        Args:
            user_id: User ID to filter by
            start: Start of time range
            end: End of time range
            limit: Maximum results to return

        Returns:
            List of Entity objects ordered by time
        """
        _, read, _ = self._get_io(user_id)

        results = read.run(
            queries.GET_TIMELINE,
            start=start.isoformat(),
            end=end.isoformat(),
            limit=limit,
        )

        entities = []
        for row in results:
            entity_data = dict(row["e"])
            entity_data["relationship_count"] = row.get("relationship_count", 0)
            entities.append(self._row_to_entity(entity_data))

        return entities

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _row_to_entity(self, data: Dict[str, Any]) -> Entity:
        """Convert row data to Entity model."""
        return Entity(
            id=data.get("id", ""),
            name=data.get("name", ""),
            type=EntityType(data.get("type", "thing")),
            user_id=data.get("user_id", ""),
            details=data.get("details"),
            icon=data.get("icon"),
            metadata=self._parse_metadata(data.get("metadata")),
            created_at=self._parse_datetime(data.get("created_at")),
            updated_at=self._parse_datetime(data.get("updated_at")),
            location=data.get("location"),
            start_time=self._parse_datetime(data.get("start_time")),
            end_time=self._parse_datetime(data.get("end_time")),
            conversation_id=data.get("conversation_id"),
            relationship_count=data.get("relationship_count"),
        )

    def _row_to_relationship(self, data: Dict[str, Any]) -> Relationship:
        """Convert row data to Relationship model."""
        rel_type = data.get("type", "RELATED_TO")
        try:
            rel_type_enum = RelationshipType(rel_type)
        except ValueError:
            rel_type_enum = RelationshipType.RELATED_TO

        return Relationship(
            id=data.get("id", ""),
            type=rel_type_enum,
            source_id=data.get("source_id", ""),
            target_id=data.get("target_id", ""),
            user_id=data.get("user_id", ""),
            context=data.get("context"),
            timestamp=self._parse_datetime(data.get("timestamp")),
            metadata=self._parse_metadata(data.get("metadata")),
            created_at=self._parse_datetime(data.get("created_at")),
            start_date=self._parse_datetime(data.get("start_date")),
            end_date=self._parse_datetime(data.get("end_date")),
            source_entity=data.get("source_entity"),
            target_entity=data.get("target_entity"),
        )

    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        """Parse datetime from graph result."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def _parse_metadata(self, value: Any) -> Dict[str, Any]:
        """Parse metadata JSON from graph result."""
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                import json

                return json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}

    # -------------------------------------------------------------------------
    # Basic Memory (MEMORY.md) — delegates to KnowledgeBaseManager
    # No FalkorDB required; works even if graph DB is down.
    # -------------------------------------------------------------------------

    def get_basic_memory(self, user_id: str) -> str:
        """Read the user's basic memory (MEMORY.md content)."""
        return self._kb.get_basic_memory(user_id)

    def write_basic_memory(self, user_id: str, content: str) -> bool:
        """Write/replace the user's basic memory (MEMORY.md content)."""
        return self._kb.write_basic_memory(user_id, content)

    async def consolidate_basic_memory(self, user_id: str, memories: list[str]) -> str:
        """Consolidate memory facts into a structured MEMORY.md."""
        return await self._kb.consolidate_basic_memory(user_id, memories)

    def shutdown(self) -> None:
        """Shutdown the service and close all per-user connections."""
        with self._io_lock:
            for client, _, _ in self._io_cache.values():
                try:
                    client.close()
                except Exception:
                    pass
            self._io_cache.clear()
        logger.info("Knowledge Graph Service shut down")

    async def test_connection(self) -> bool:
        """Test FalkorDB connection via a probe graph (no per-user select)."""
        try:
            probe = GraphClient(
                host=self.host, port=self.port, graph_name="_chronicle_probe"
            )
            try:
                # Writable session: ``ro_query`` on an empty Redis key
                # raises "Invalid graph operation on empty key".
                probe.session().run("RETURN 1 AS test")
                return True
            finally:
                probe.close()
        except Exception as e:
            logger.error(f"FalkorDB connection test failed: {e}")
            return False


def get_knowledge_graph_service() -> KnowledgeGraphService:
    """Get the global knowledge graph service instance.

    Returns:
        KnowledgeGraphService singleton instance
    """
    global _knowledge_graph_service

    if _knowledge_graph_service is None:
        with _service_lock:
            if _knowledge_graph_service is None:
                _knowledge_graph_service = KnowledgeGraphService()
                logger.info("Knowledge Graph Service created")

    return _knowledge_graph_service


def shutdown_knowledge_graph_service() -> None:
    """Shutdown the global knowledge graph service."""
    global _knowledge_graph_service

    if _knowledge_graph_service is not None:
        _knowledge_graph_service.shutdown()
        _knowledge_graph_service = None
