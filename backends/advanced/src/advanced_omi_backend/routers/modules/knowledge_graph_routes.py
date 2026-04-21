"""
Knowledge Graph API routes for Chronicle.

Handles entity, relationship, conversation doc, and timeline operations.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.annotation import (
    Annotation,
    AnnotationStatus,
    AnnotationType,
)
from advanced_omi_backend.services.knowledge_graph import (
    KnowledgeGraphService,
    get_knowledge_graph_service,
)
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge-graph", tags=["knowledge-graph"])


# =============================================================================
# REQUEST MODELS
# =============================================================================


class UpdateEntityRequest(BaseModel):
    """Request model for updating entity fields."""

    name: Optional[str] = None
    details: Optional[str] = None
    icon: Optional[str] = None


class UpdatePromiseRequest(BaseModel):
    """Request model for updating promise status."""

    status: str  # pending, in_progress, completed, cancelled


class UpdateKBRequest(BaseModel):
    """Request model for updating the user's basic memory."""

    content: str


# =============================================================================
# ENTITY ENDPOINTS
# =============================================================================


@router.get("/entities")
async def get_entities(
    current_user: User = Depends(current_active_user),
    entity_type: Optional[str] = Query(
        default=None,
        description="Filter by entity type (person, place, organization, event, thing)",
    ),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Get all entities for the current user.

    Optionally filter by entity type. Returns entities with their
    relationship counts.
    """
    try:
        service = get_knowledge_graph_service()
        entities = await service.get_entities(
            user_id=str(current_user.id),
            entity_type=entity_type,
            limit=limit,
        )

        return {
            "entities": [e.to_dict() for e in entities],
            "count": len(entities),
            "user_id": str(current_user.id),
        }
    except Exception as e:
        logger.error(f"Error getting entities: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error getting entities: {str(e)}"},
        )


@router.get("/entities/{entity_id}")
async def get_entity(
    entity_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get a single entity by ID with its relationship count."""
    try:
        service = get_knowledge_graph_service()
        entity = await service.get_entity(
            entity_id=entity_id,
            user_id=str(current_user.id),
        )

        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        return {"entity": entity.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting entity {entity_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error getting entity: {str(e)}"},
        )


@router.get("/entities/{entity_id}/relationships")
async def get_entity_relationships(
    entity_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all relationships for an entity.

    Returns both incoming and outgoing relationships with
    connected entity information.
    """
    try:
        service = get_knowledge_graph_service()

        # First verify entity exists
        entity = await service.get_entity(
            entity_id=entity_id,
            user_id=str(current_user.id),
        )

        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        relationships = await service.get_entity_relationships(
            entity_id=entity_id,
            user_id=str(current_user.id),
        )

        return {
            "entity": entity.to_dict(),
            "relationships": [r.to_dict() for r in relationships],
            "count": len(relationships),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting relationships for {entity_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error getting relationships: {str(e)}"},
        )


@router.patch("/entities/{entity_id}")
async def update_entity(
    entity_id: str,
    request: UpdateEntityRequest,
    current_user: User = Depends(current_active_user),
):
    """Update an entity's name, details, or icon.

    Also creates entity annotations as a side effect for each changed field.
    These annotations feed the jargon and entity extraction pipelines.
    """
    try:
        if request.name is None and request.details is None and request.icon is None:
            raise HTTPException(
                status_code=400,
                detail="At least one field (name, details, icon) must be provided",
            )

        service = get_knowledge_graph_service()

        # Get current entity for annotation original values
        existing = await service.get_entity(
            entity_id=entity_id,
            user_id=str(current_user.id),
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Entity not found")

        # Apply update to FalkorDB
        updated = await service.update_entity(
            entity_id=entity_id,
            user_id=str(current_user.id),
            name=request.name,
            details=request.details,
            icon=request.icon,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Entity not found")

        # Create annotations for changed text fields (name, details)
        # These feed the jargon pipeline and entity extraction pipeline.
        # Icon changes don't create annotations (not text corrections).
        for field in ("name", "details"):
            new_value = getattr(request, field)
            if new_value is not None:
                old_value = getattr(existing, field) or ""
                annotation = Annotation(
                    annotation_type=AnnotationType.ENTITY,
                    user_id=str(current_user.id),
                    entity_id=entity_id,
                    entity_field=field,
                    original_text=old_value,
                    corrected_text=new_value,
                    status=AnnotationStatus.ACCEPTED,
                    processed=False,
                )
                await annotation.save()
                logger.info(
                    f"Created entity annotation for {field} change on entity {entity_id}"
                )

        return {"entity": updated.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating entity {entity_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error updating entity: {str(e)}"},
        )


@router.delete("/entities/{entity_id}")
async def delete_entity(
    entity_id: str,
    current_user: User = Depends(current_active_user),
):
    """Delete an entity and all its relationships."""
    try:
        service = get_knowledge_graph_service()
        deleted = await service.delete_entity(
            entity_id=entity_id,
            user_id=str(current_user.id),
        )

        if not deleted:
            raise HTTPException(status_code=404, detail="Entity not found")

        return {"message": "Entity deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting entity {entity_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error deleting entity: {str(e)}"},
        )


# =============================================================================
# SEARCH ENDPOINT
# =============================================================================


@router.get("/search")
async def search_entities(
    query: str = Query(..., description="Search query for entity names and details"),
    current_user: User = Depends(current_active_user),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Search entities by name or details.

    Performs case-insensitive substring matching on entity names
    and details fields.
    """
    try:
        service = get_knowledge_graph_service()
        entities = await service.search_entities(
            query=query,
            user_id=str(current_user.id),
            limit=limit,
        )

        return {
            "query": query,
            "entities": [e.to_dict() for e in entities],
            "count": len(entities),
        }
    except Exception as e:
        logger.error(f"Error searching entities: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error searching entities: {str(e)}"},
        )


# =============================================================================
# CONVERSATION DOC ENDPOINTS
# =============================================================================


@router.get("/conversations")
async def get_conversation_docs(
    current_user: User = Depends(current_active_user),
    person: Optional[str] = Query(
        default=None,
        description="Filter by person name (case-insensitive substring match)",
    ),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Get conversation documents with linked people.

    Returns ConvDoc nodes from the chronicle memory system with
    their titles, summaries, dates, and mentioned people.
    Optionally filter by person name.
    """
    try:
        service = get_knowledge_graph_service()
        docs = await service.get_conversation_docs(
            user_id=str(current_user.id),
            person=person,
            limit=limit,
        )

        return {
            "conversations": docs,
            "count": len(docs),
        }
    except Exception as e:
        logger.error(f"Error getting conversation docs: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error getting conversation docs: {str(e)}"},
        )


@router.get("/people")
async def get_people(
    current_user: User = Depends(current_active_user),
):
    """Get distinct people mentioned across conversation documents.

    Returns ConvEntity names with descriptions and mention counts,
    ordered by mention count descending. Used for filter dropdowns.
    """
    try:
        service = get_knowledge_graph_service()
        people = await service.get_people(
            user_id=str(current_user.id),
        )

        return {
            "people": people,
            "count": len(people),
        }
    except Exception as e:
        logger.error(f"Error getting people: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error getting people: {str(e)}"},
        )


# =============================================================================
# TIMELINE ENDPOINT
# =============================================================================


@router.get("/timeline")
async def get_timeline(
    start: str = Query(..., description="Start date (ISO format)"),
    end: str = Query(..., description="End date (ISO format)"),
    current_user: User = Depends(current_active_user),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Get entities within a time range.

    Returns entities ordered by their start_time or created_at date.
    Useful for building timeline visualizations.
    """
    try:
        # Parse dates
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SS): {e}",
            )

        if start_dt > end_dt:
            raise HTTPException(
                status_code=400,
                detail="Start date must be before end date",
            )

        service = get_knowledge_graph_service()
        entities = await service.get_timeline(
            user_id=str(current_user.id),
            start=start_dt,
            end=end_dt,
            limit=limit,
        )

        return {
            "start": start,
            "end": end,
            "entities": [e.to_dict() for e in entities],
            "count": len(entities),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting timeline: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error getting timeline: {str(e)}"},
        )


# =============================================================================
# BASIC MEMORY (MEMORY.md)
# =============================================================================


@router.get("/kb")
async def get_knowledge_base(
    current_user: User = Depends(current_active_user),
):
    """Get the user's basic memory (MEMORY.md content)."""
    try:
        service = get_knowledge_graph_service()
        content = service.get_basic_memory(user_id=str(current_user.id))
        return {"content": content, "user_id": str(current_user.id)}
    except Exception as e:
        logger.error(f"Error reading basic memory: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error reading basic memory: {str(e)}"},
        )


@router.put("/kb")
async def update_knowledge_base(
    request: UpdateKBRequest,
    current_user: User = Depends(current_active_user),
):
    """Write/update the user's basic memory (MEMORY.md content)."""
    try:
        service = get_knowledge_graph_service()
        success = service.write_basic_memory(
            user_id=str(current_user.id),
            content=request.content,
        )
        if not success:
            return JSONResponse(
                status_code=500,
                content={"message": "Failed to write basic memory"},
            )
        return {"message": "Basic memory updated", "user_id": str(current_user.id)}
    except Exception as e:
        logger.error(f"Error writing basic memory: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error writing basic memory: {str(e)}"},
        )


@router.post("/kb/consolidate")
async def consolidate_knowledge_base(
    current_user: User = Depends(current_active_user),
):
    """Consolidate all user memories into a structured MEMORY.md.

    Reads all extracted facts from the memory store, sends them to the
    LLM with the existing MEMORY.md, and writes back a merged document.
    """
    from advanced_omi_backend.services.memory import get_memory_service

    try:
        user_id = str(current_user.id)
        memory_service = get_memory_service()
        all_memories = await memory_service.get_all_memories(user_id=user_id, limit=500)
        facts = [m.content for m in all_memories if m.content]

        if not facts:
            return JSONResponse(
                status_code=400,
                content={"message": "No memories found to consolidate"},
            )

        service = get_knowledge_graph_service()
        updated_content = await service.consolidate_basic_memory(user_id, facts)

        return {
            "message": "Basic memory consolidated",
            "user_id": user_id,
            "facts_processed": len(facts),
            "content_length": len(updated_content),
        }
    except Exception as e:
        logger.error(f"Error consolidating basic memory: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error consolidating basic memory: {str(e)}"},
        )


# =============================================================================
# HEALTH CHECK
# =============================================================================


@router.get("/health")
async def knowledge_graph_health():
    """Check knowledge graph service health.

    Tests FalkorDB connection and returns status.
    """
    try:
        service = get_knowledge_graph_service()
        is_healthy = await service.test_connection()

        if is_healthy:
            return {"status": "healthy", "falkordb": "connected"}
        else:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "falkordb": "disconnected"},
            )
    except Exception as e:
        logger.error(f"Knowledge graph health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)},
        )
