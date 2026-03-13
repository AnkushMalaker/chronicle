"""
Memory management routes for Chronicle API.

Handles memory CRUD operations, search, and debug functionality.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user, current_superuser
from advanced_omi_backend.controllers import memory_controller
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])


class AddMemoryRequest(BaseModel):
    """Request model for adding a memory."""

    content: str
    source_id: Optional[str] = None


@router.get("")
async def get_memories(
    current_user: User = Depends(current_active_user),
    limit: int = Query(default=50, ge=1, le=1000),
    user_id: Optional[str] = Query(
        default=None, description="User ID filter (admin only)"
    ),
):
    """Get memories. Users see only their own memories, admins can see all or filter by user."""
    return await memory_controller.get_memories(current_user, limit, user_id)


@router.get("/with-transcripts")
async def get_memories_with_transcripts(
    current_user: User = Depends(current_active_user),
    limit: int = Query(default=50, ge=1, le=1000),
    user_id: Optional[str] = Query(
        default=None, description="User ID filter (admin only)"
    ),
):
    """Get memories with their source transcripts. Users see only their own memories, admins can see all or filter by user."""
    return await memory_controller.get_memories_with_transcripts(
        current_user, limit, user_id
    )


@router.get("/search")
async def search_memories(
    query: str = Query(..., description="Search query"),
    current_user: User = Depends(current_active_user),
    limit: int = Query(default=20, ge=1, le=100),
    score_threshold: float = Query(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum similarity score (0.0 = no threshold)",
    ),
    user_id: Optional[str] = Query(
        default=None, description="User ID filter (admin only)"
    ),
):
    """Search memories by text query with configurable similarity threshold. Users can only search their own memories, admins can search all or filter by user."""
    return await memory_controller.search_memories(
        query, current_user, limit, score_threshold, user_id
    )


@router.post("")
async def add_memory(
    request: AddMemoryRequest, current_user: User = Depends(current_active_user)
):
    """Add a memory directly from content text. The service will extract structured memories from the provided content."""
    return await memory_controller.add_memory(
        request.content, current_user, request.source_id
    )


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str, current_user: User = Depends(current_active_user)
):
    """Delete a memory by ID. Users can only delete their own memories, admins can delete any."""
    return await memory_controller.delete_memory(memory_id, current_user)


@router.get("/admin")
async def get_all_memories_admin(
    current_user: User = Depends(current_superuser), limit: int = 200
):
    """Get all memories across all users for admin review. Admin only."""
    return await memory_controller.get_all_memories_admin(current_user, limit)


@router.get("/{memory_id}")
async def get_memory_by_id(
    memory_id: str,
    current_user: User = Depends(current_active_user),
    user_id: Optional[str] = Query(
        default=None, description="User ID filter (admin only)"
    ),
):
    """Get a single memory by ID. Users can only access their own memories, admins can access any."""
    return await memory_controller.get_memory_by_id(memory_id, current_user, user_id)
