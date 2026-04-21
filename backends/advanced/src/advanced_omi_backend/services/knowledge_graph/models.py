"""Pydantic models for Knowledge Graph entities and relationships.

This module defines the data structures used throughout the knowledge graph
service for storing and retrieving entities and relationships.
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class EntityType(str, Enum):
    """Supported entity types in the knowledge graph."""

    PERSON = "person"
    PLACE = "place"
    ORGANIZATION = "organization"
    EVENT = "event"
    CONVERSATION = "conversation"
    FACT = "fact"
    THING = "thing"  # Generic fallback


class RelationshipType(str, Enum):
    """Supported relationship types between entities."""

    MENTIONED_IN = "MENTIONED_IN"
    WORKS_AT = "WORKS_AT"
    LIVES_IN = "LIVES_IN"
    KNOWS = "KNOWS"
    EXTRACTED_FROM = "EXTRACTED_FROM"
    ABOUT = "ABOUT"
    ATTENDED = "ATTENDED"
    LOCATED_AT = "LOCATED_AT"
    PART_OF = "PART_OF"
    RELATED_TO = "RELATED_TO"


class Entity(BaseModel):
    """Represents an entity in the knowledge graph.

    Entities are nodes in the graph representing people, places, events, etc.
    Each entity belongs to a specific user and can have relationships with
    other entities.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    type: EntityType
    user_id: str
    details: Optional[str] = None
    icon: Optional[str] = None  # Emoji for display
    embedding: Optional[List[float]] = None  # For semantic search
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Type-specific fields
    location: Optional[Dict[str, float]] = None  # For Place: {lat, lon}
    start_time: Optional[datetime] = None  # For Event
    end_time: Optional[datetime] = None  # For Event
    conversation_id: Optional[str] = None  # For Conversation entity

    # Populated when fetching with relationships
    relationships: Optional[List["Relationship"]] = None
    relationship_count: Optional[int] = None

    class Config:
        use_enum_values = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "user_id": self.user_id,
            "details": self.details,
            "icon": self.icon,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if self.location:
            data["location"] = self.location
        if self.start_time:
            data["start_time"] = self.start_time.isoformat()
        if self.end_time:
            data["end_time"] = self.end_time.isoformat()
        if self.conversation_id:
            data["conversation_id"] = self.conversation_id
        if self.relationships is not None:
            data["relationships"] = [r.to_dict() for r in self.relationships]
        if self.relationship_count is not None:
            data["relationship_count"] = self.relationship_count
        return data


class Relationship(BaseModel):
    """Represents a relationship between two entities.

    Relationships are edges in the graph connecting entities with
    typed connections (e.g., WORKS_AT, KNOWS, MENTIONED_IN).
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: RelationshipType
    source_id: str  # Entity ID
    target_id: str  # Entity ID
    user_id: str
    context: Optional[str] = None  # Additional context about the relationship
    timestamp: Optional[datetime] = None  # When was this relationship established
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # For start/end dates on temporal relationships
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None

    # Populated when fetching relationships
    source_entity: Optional[Entity] = None
    target_entity: Optional[Entity] = None

    class Config:
        use_enum_values = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "id": self.id,
            "type": self.type,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "user_id": self.user_id,
            "context": self.context,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if self.start_date:
            data["start_date"] = self.start_date.isoformat()
        if self.end_date:
            data["end_date"] = self.end_date.isoformat()
        if self.source_entity:
            data["source_entity"] = self.source_entity.to_dict()
        if self.target_entity:
            data["target_entity"] = self.target_entity.to_dict()
        return data


class ExtractedEntity(BaseModel):
    """Entity as extracted by LLM before graph storage."""

    name: str
    type: str  # Will be validated against EntityType
    details: Optional[str] = None
    icon: Optional[str] = None
    # For events
    when: Optional[str] = None  # Natural language time reference


class ExtractedRelationship(BaseModel):
    """Relationship as extracted by LLM."""

    subject: str  # Entity name or "speaker"
    relation: str  # Relationship type
    object: str  # Entity name


class ExtractionResult(BaseModel):
    """Result of entity extraction from a conversation."""

    entities: List[ExtractedEntity] = Field(default_factory=list)
    relationships: List[ExtractedRelationship] = Field(default_factory=list)

    # Populated after storage
    stored_entity_ids: List[str] = Field(default_factory=list)
    stored_relationship_ids: List[str] = Field(default_factory=list)
