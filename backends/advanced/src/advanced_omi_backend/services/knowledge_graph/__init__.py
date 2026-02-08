"""Knowledge Graph service for entity and relationship extraction.

This module provides:
- Entity extraction from conversations using LLM
- Storage of entities and relationships in Neo4j
- API for querying knowledge graph (entities, relationships, promises, timeline)
"""

from .models import (
    Entity,
    EntityType,
    ExtractionResult,
    Promise,
    PromiseStatus,
    Relationship,
    RelationshipType,
)
from .service import KnowledgeGraphService, get_knowledge_graph_service

__all__ = [
    "Entity",
    "EntityType",
    "Relationship",
    "RelationshipType",
    "Promise",
    "PromiseStatus",
    "ExtractionResult",
    "KnowledgeGraphService",
    "get_knowledge_graph_service",
]
