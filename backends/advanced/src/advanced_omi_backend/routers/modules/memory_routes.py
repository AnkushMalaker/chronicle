"""
Memory management routes for Chronicle API.

Handles memory CRUD operations, search, and debug functionality.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user, current_superuser
from advanced_omi_backend.controllers import memory_controller
from advanced_omi_backend.services.memory.person_merge import (
    PersonMergeError,
    PersonMergeStale,
)
from advanced_omi_backend.services.memory.person_merge_actions import (
    apply_person_merge,
    get_person_suggestions,
    preview_person_merge,
    set_people_distinct,
)
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])


class AddMemoryRequest(BaseModel):
    """Request model for adding a memory."""

    content: str
    source_id: Optional[str] = None


class PersonMergePreviewRequest(BaseModel):
    """Local state supplied by an Obsidian or automation client."""

    source_name: str
    target_name: str
    source_hash: Optional[str] = None
    target_hash: Optional[str] = None


class PersonMergeApplyRequest(BaseModel):
    """Apply the exact server-side plan previously shown to the user."""

    source_name: str
    target_name: str
    plan_token: str


class PersonIdentityDecisionRequest(BaseModel):
    """A durable user decision about whether two person notes are distinct."""

    person_a: str
    person_b: str
    decision: Literal["distinct", "clear_distinct"]
    revision: Optional[str] = None


def _person_merge_http_error(error: PersonMergeError) -> HTTPException:
    status = 409 if isinstance(error, PersonMergeStale) else 422
    return HTTPException(status_code=status, detail=str(error))


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


@router.get("/audit")
async def get_memory_audit(
    current_user: User = Depends(current_active_user),
    limit: int = Query(default=100, ge=1, le=1000),
    conversation_id: Optional[str] = Query(
        default=None, description="Filter to a single conversation"
    ),
    user_id: Optional[str] = Query(
        default=None, description="User ID filter (admin only)"
    ),
):
    """Memory vault change ledger (audit history). Newest first."""
    return await memory_controller.get_memory_audit(
        current_user, limit, user_id, conversation_id
    )


@router.get("/audit/{entry_id}/diff")
async def get_memory_audit_diff(
    entry_id: str,
    current_user: User = Depends(current_active_user),
):
    """Before→after diff for one ledger entry (reconstructs the prior note state)."""
    return await memory_controller.get_memory_audit_diff(current_user, entry_id)


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


@router.post("/people/merge/preview")
async def preview_people_merge(
    request: PersonMergePreviewRequest,
    current_user: User = Depends(current_active_user),
):
    """Preview a deterministic person merge without changing the vault."""
    try:
        return await preview_person_merge(
            current_user.user_id,
            request.source_name,
            request.target_name,
            request.source_hash,
            request.target_hash,
        )
    except PersonMergeError as error:
        raise _person_merge_http_error(error) from error


@router.post("/people/merge")
async def merge_people(
    request: PersonMergeApplyRequest,
    current_user: User = Depends(current_active_user),
):
    """Apply a previously previewed deterministic person merge."""
    try:
        return await apply_person_merge(
            current_user.user_id,
            request.source_name,
            request.target_name,
            request.plan_token,
        )
    except PersonMergeError as error:
        raise _person_merge_http_error(error) from error


@router.get("/people/suggestions")
async def get_people_suggestions(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(current_active_user),
):
    """Return deterministic duplicate-person candidates for user review."""
    return {
        "suggestions": await get_person_suggestions(current_user.user_id, limit),
    }


@router.post("/people/identity")
async def set_people_identity(
    request: PersonIdentityDecisionRequest,
    current_user: User = Depends(current_active_user),
):
    """Persist or clear a symmetric distinct-person annotation."""
    try:
        return await set_people_distinct(
            current_user.user_id,
            request.person_a,
            request.person_b,
            distinct=request.decision == "distinct",
            revision=request.revision,
        )
    except PersonMergeError as error:
        raise _person_merge_http_error(error) from error


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
