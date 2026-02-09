"""Knowledge Graph Service for entity and relationship management.

This module provides the main service for:
- Extracting entities and relationships from conversations
- Storing and retrieving entities from Neo4j
- Managing promises and tracking their status
- Querying the knowledge graph
"""

import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..neo4j_client import Neo4jClient, Neo4jReadInterface, Neo4jWriteInterface
from . import queries
from .entity_extractor import extract_entities_from_transcript, parse_natural_datetime
from .models import (
    Entity,
    EntityType,
    ExtractionResult,
    Promise,
    PromiseStatus,
    Relationship,
    RelationshipType,
)

logger = logging.getLogger("knowledge_graph")

# Global service instance
_knowledge_graph_service: Optional["KnowledgeGraphService"] = None
_service_lock = threading.Lock()


class KnowledgeGraphService:
    """Service for managing knowledge graph entities and relationships.

    This service handles:
    - Entity extraction from conversation transcripts
    - CRUD operations on entities and relationships
    - Promise tracking and management
    - Graph queries (timeline, search, related entities)
    """

    def __init__(
        self,
        neo4j_uri: Optional[str] = None,
        neo4j_user: Optional[str] = None,
        neo4j_password: Optional[str] = None,
    ):
        """Initialize the knowledge graph service.

        Args:
            neo4j_uri: Neo4j connection URI (defaults to env var)
            neo4j_user: Neo4j username (defaults to env var)
            neo4j_password: Neo4j password (defaults to env var)
        """
        # Construct URI from host if URI not provided
        if neo4j_uri:
            self.neo4j_uri = neo4j_uri
        elif os.getenv("NEO4J_URI"):
            self.neo4j_uri = os.getenv("NEO4J_URI")
        else:
            neo4j_host = os.getenv("NEO4J_HOST", "neo4j")
            self.neo4j_uri = f"bolt://{neo4j_host}:7687"

        self.neo4j_user = neo4j_user or os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password = neo4j_password or os.getenv("NEO4J_PASSWORD", "password")

        self._client: Optional[Neo4jClient] = None
        self._read: Optional[Neo4jReadInterface] = None
        self._write: Optional[Neo4jWriteInterface] = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Ensure Neo4j client is initialized."""
        if not self._initialized:
            self._client = Neo4jClient(
                uri=self.neo4j_uri,
                user=self.neo4j_user,
                password=self.neo4j_password,
            )
            self._read = Neo4jReadInterface(self._client)
            self._write = Neo4jWriteInterface(self._client)
            self._initialized = True
            logger.info("Knowledge Graph Service initialized with Neo4j connection")

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
        self._ensure_initialized()

        if not transcript or not transcript.strip():
            logger.debug(f"Empty transcript for conversation {conversation_id}")
            return {"entities": 0, "relationships": 0, "promises": 0}

        try:
            # Extract entities using LLM
            extraction = await extract_entities_from_transcript(
                transcript=transcript,
                conversation_id=conversation_id,
            )

            if not extraction.entities and not extraction.promises:
                logger.debug(f"No entities extracted from conversation {conversation_id}")
                return {"entities": 0, "relationships": 0, "promises": 0}

            # Create conversation entity node
            conv_entity_id = await self._create_conversation_entity(
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

            # Store promises
            promise_count = await self._store_promises(
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

            logger.info(
                f"Processed conversation {conversation_id}: "
                f"{len(entity_id_map)} entities, {rel_count} relationships, {promise_count} promises"
            )

            return {
                "entities": len(entity_id_map),
                "relationships": rel_count,
                "promises": promise_count,
                "entity_ids": list(entity_id_map.values()),
            }

        except Exception as e:
            logger.error(f"Error processing conversation {conversation_id}: {e}")
            return {"entities": 0, "relationships": 0, "promises": 0, "error": str(e)}

    async def _create_conversation_entity(
        self,
        conversation_id: str,
        user_id: str,
        name: str,
    ) -> str:
        """Create or update a conversation entity node."""
        entity_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

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

        self._write.run(queries.CREATE_CONVERSATION_ENTITY, **params)
        return entity_id

    async def _store_entities(
        self,
        extraction: ExtractionResult,
        user_id: str,
        conversation_id: str,
    ) -> Dict[str, str]:
        """Store extracted entities in Neo4j.

        Returns:
            Mapping of entity name (lowercase) to entity ID
        """
        entity_id_map: Dict[str, str] = {}
        now = datetime.utcnow().isoformat()

        for extracted in extraction.entities:
            # Check if entity already exists for this user
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

            self._write.run(queries.CREATE_ENTITY_SIMPLE, **params)
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
        """Store extracted relationships in Neo4j."""
        count = 0
        now = datetime.utcnow().isoformat()

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

            self._write.run(queries.CREATE_RELATIONSHIP, **params)
            extraction.stored_relationship_ids.append(rel_id)
            count += 1

        return count

    async def _store_promises(
        self,
        extraction: ExtractionResult,
        user_id: str,
        entity_id_map: Dict[str, str],
        conversation_id: str,
    ) -> int:
        """Store extracted promises in Neo4j."""
        count = 0
        now = datetime.utcnow().isoformat()

        for promise in extraction.promises:
            promise_id = str(uuid.uuid4())

            # Find target entity if specified
            to_entity_id = None
            to_entity_name = promise.to
            if promise.to:
                to_entity_id = entity_id_map.get(promise.to.lower())

            # Parse deadline
            due_date = None
            if promise.deadline:
                parsed = parse_natural_datetime(promise.deadline)
                if parsed:
                    due_date = parsed.isoformat()

            params = {
                "id": promise_id,
                "user_id": user_id,
                "action": promise.action,
                "to_entity_id": to_entity_id,
                "to_entity_name": to_entity_name,
                "status": PromiseStatus.PENDING.value,
                "due_date": due_date,
                "completed_at": None,
                "source_conversation_id": conversation_id,
                "context": None,
                "metadata": "{}",
                "created_at": now,
                "updated_at": now,
            }

            self._write.run(queries.CREATE_PROMISE, **params)
            extraction.stored_promise_ids.append(promise_id)
            count += 1

        return count

    async def _link_entities_to_conversation(
        self,
        entity_ids: List[str],
        conversation_id: str,
        user_id: str,
    ) -> None:
        """Link entities to their source conversation."""
        now = datetime.utcnow().isoformat()

        for entity_id in entity_ids:
            params = {
                "entity_id": entity_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "rel_id": str(uuid.uuid4()),
                "timestamp": now,
                "context": None,
            }
            self._write.run(queries.LINK_ENTITY_TO_CONVERSATION, **params)

    def _find_entity_by_name(self, name: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Find existing entity by name for a user."""
        results = self._read.run(
            queries.FIND_ENTITY_BY_NAME,
            name=name,
            user_id=user_id,
        )
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
        self._ensure_initialized()

        results = self._read.run(
            queries.GET_ENTITIES_BY_USER,
            user_id=user_id,
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
        self._ensure_initialized()

        results = self._read.run(
            queries.GET_ENTITY_BY_ID,
            id=entity_id,
            user_id=user_id,
        )

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
        self._ensure_initialized()

        results = self._read.run(
            queries.GET_ENTITY_RELATIONSHIPS,
            entity_id=entity_id,
            user_id=user_id,
        )

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
        self._ensure_initialized()

        results = self._read.run(
            queries.SEARCH_ENTITIES_BY_NAME,
            query=query,
            user_id=user_id,
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
        name: str = None,
        details: str = None,
        icon: str = None,
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
        self._ensure_initialized()

        results = self._write.run(
            queries.UPDATE_ENTITY,
            id=entity_id,
            user_id=user_id,
            name=name,
            details=details,
            icon=icon,
            metadata=None,
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
        self._ensure_initialized()

        results = self._write.run(
            queries.DELETE_ENTITY,
            id=entity_id,
            user_id=user_id,
        )

        deleted = results[0]["deleted_count"] if results else 0
        return deleted > 0

    # =========================================================================
    # PROMISE OPERATIONS
    # =========================================================================

    async def get_promises(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Promise]:
        """Get promises for a user.

        Args:
            user_id: User ID to filter by
            status: Optional status filter (pending, completed, etc.)
            limit: Maximum results to return

        Returns:
            List of Promise objects
        """
        self._ensure_initialized()

        results = self._read.run(
            queries.GET_PROMISES_BY_USER,
            user_id=user_id,
            status=status,
            limit=limit,
        )

        promises = []
        for row in results:
            promise_data = dict(row["p"])
            if row.get("target"):
                promise_data["to_entity_name"] = row["target"].get("name")
            promises.append(self._row_to_promise(promise_data))

        return promises

    async def update_promise_status(
        self,
        promise_id: str,
        user_id: str,
        status: str,
    ) -> Optional[Promise]:
        """Update a promise's status.

        Args:
            promise_id: Promise UUID
            user_id: User ID for permission check
            status: New status value

        Returns:
            Updated Promise object or None if not found
        """
        self._ensure_initialized()

        results = self._write.run(
            queries.UPDATE_PROMISE_STATUS,
            id=promise_id,
            user_id=user_id,
            status=status,
        )

        if not results:
            return None

        return self._row_to_promise(dict(results[0]["p"]))

    async def delete_promise(
        self,
        promise_id: str,
        user_id: str,
    ) -> bool:
        """Delete a promise.

        Args:
            promise_id: Promise UUID to delete
            user_id: User ID for permission check

        Returns:
            True if deleted, False if not found
        """
        self._ensure_initialized()

        results = self._write.run(
            queries.DELETE_PROMISE,
            id=promise_id,
            user_id=user_id,
        )

        deleted = results[0]["deleted_count"] if results else 0
        return deleted > 0

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
        self._ensure_initialized()

        results = self._read.run(
            queries.GET_TIMELINE,
            user_id=user_id,
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
        """Convert Neo4j row data to Entity model."""
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
        """Convert Neo4j row data to Relationship model."""
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

    def _row_to_promise(self, data: Dict[str, Any]) -> Promise:
        """Convert Neo4j row data to Promise model."""
        status = data.get("status", "pending")
        try:
            status_enum = PromiseStatus(status)
        except ValueError:
            status_enum = PromiseStatus.PENDING

        return Promise(
            id=data.get("id", ""),
            user_id=data.get("user_id", ""),
            action=data.get("action", ""),
            to_entity_id=data.get("to_entity_id"),
            to_entity_name=data.get("to_entity_name"),
            status=status_enum,
            due_date=self._parse_datetime(data.get("due_date")),
            completed_at=self._parse_datetime(data.get("completed_at")),
            source_conversation_id=data.get("source_conversation_id"),
            context=data.get("context"),
            metadata=self._parse_metadata(data.get("metadata")),
            created_at=self._parse_datetime(data.get("created_at")),
            updated_at=self._parse_datetime(data.get("updated_at")),
        )

    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        """Parse datetime from Neo4j."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        # Neo4j DateTime object
        if hasattr(value, "to_native"):
            return value.to_native()
        return None

    def _parse_metadata(self, value: Any) -> Dict[str, Any]:
        """Parse metadata JSON from Neo4j."""
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

    def shutdown(self) -> None:
        """Shutdown the service and close connections."""
        if self._client:
            self._client.close()
            self._client = None
        self._initialized = False
        logger.info("Knowledge Graph Service shut down")

    async def test_connection(self) -> bool:
        """Test Neo4j connection."""
        try:
            self._ensure_initialized()
            # Simple query to test connection
            self._read.run("RETURN 1 as test")
            return True
        except Exception as e:
            logger.error(f"Neo4j connection test failed: {e}")
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
