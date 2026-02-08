"""
Knowledge Graph API routes for Chronicle.

Handles entity, relationship, promise, and timeline operations.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.services.knowledge_graph import (
    KnowledgeGraphService,
    PromiseStatus,
    get_knowledge_graph_service,
)
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge-graph", tags=["knowledge-graph"])


# =============================================================================
# REQUEST MODELS
# =============================================================================


class UpdatePromiseRequest(BaseModel):
    """Request model for updating promise status."""
    status: str  # pending, in_progress, completed, cancelled


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
# PROMISE ENDPOINTS
# =============================================================================


@router.get("/promises")
async def get_promises(
    current_user: User = Depends(current_active_user),
    status: Optional[str] = Query(
        default=None,
        description="Filter by status (pending, in_progress, completed, cancelled)",
    ),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Get promises/tasks for the current user.

    Optionally filter by status. Returns promises ordered by
    due date (ascending) then created date (descending).
    """
    try:
        # Validate status if provided
        if status:
            try:
                PromiseStatus(status)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status. Must be one of: {[s.value for s in PromiseStatus]}",
                )

        service = get_knowledge_graph_service()
        promises = await service.get_promises(
            user_id=str(current_user.id),
            status=status,
            limit=limit,
        )

        return {
            "promises": [p.to_dict() for p in promises],
            "count": len(promises),
            "user_id": str(current_user.id),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting promises: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error getting promises: {str(e)}"},
        )


@router.patch("/promises/{promise_id}")
async def update_promise_status(
    promise_id: str,
    request: UpdatePromiseRequest,
    current_user: User = Depends(current_active_user),
):
    """Update a promise's status.

    Valid statuses: pending, in_progress, completed, cancelled
    """
    try:
        # Validate status
        try:
            PromiseStatus(request.status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Must be one of: {[s.value for s in PromiseStatus]}",
            )

        service = get_knowledge_graph_service()
        promise = await service.update_promise_status(
            promise_id=promise_id,
            user_id=str(current_user.id),
            status=request.status,
        )

        if not promise:
            raise HTTPException(status_code=404, detail="Promise not found")

        return {"promise": promise.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating promise {promise_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error updating promise: {str(e)}"},
        )


@router.delete("/promises/{promise_id}")
async def delete_promise(
    promise_id: str,
    current_user: User = Depends(current_active_user),
):
    """Delete a promise."""
    try:
        service = get_knowledge_graph_service()
        deleted = await service.delete_promise(
            promise_id=promise_id,
            user_id=str(current_user.id),
        )

        if not deleted:
            raise HTTPException(status_code=404, detail="Promise not found")

        return {"message": "Promise deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting promise {promise_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error deleting promise: {str(e)}"},
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
# HEALTH CHECK
# =============================================================================


@router.get("/health")
async def knowledge_graph_health():
    """Check knowledge graph service health.

    Tests Neo4j connection and returns status.
    """
    try:
        service = get_knowledge_graph_service()
        is_healthy = await service.test_connection()

        if is_healthy:
            return {"status": "healthy", "neo4j": "connected"}
        else:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "neo4j": "disconnected"},
            )
    except Exception as e:
        logger.error(f"Knowledge graph health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)},
        )
