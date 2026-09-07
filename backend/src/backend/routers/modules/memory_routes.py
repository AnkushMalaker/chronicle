"""
Memory management routes for Chronicle API.

Handles memory CRUD operations, search, and debug functionality.
"""

import logging
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from backend.auth import current_active_user, current_superuser
from backend.controllers import memory_controller
from backend.services.memory.agent.operating_memory import (
    OperatingMemoryStore,
    VaultToolError,
)
from backend.services.memory.person_merge import PersonMergeError, PersonMergeStale
from backend.services.memory.person_merge_actions import (
    apply_person_merge,
    get_person_suggestions,
    preview_person_merge,
    set_people_distinct,
)
from backend.services.observability.system_events import record_event
from backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])


async def _run_blocking(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one filesystem operation on Starlette's managed worker pool."""

    return await run_in_threadpool(function, *args, **kwargs)


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


class OperatingMemoryReviewRequest(BaseModel):
    """Human evaluation decision for one shadow AGENTS.md candidate."""

    decision: Literal["approve", "reject"]
    rationale: str
    evidence_ids: list[str]


def _person_merge_http_error(error: PersonMergeError) -> HTTPException:
    status = 409 if isinstance(error, PersonMergeStale) else 422
    return HTTPException(status_code=status, detail=str(error))


def _operating_memory_http_error(error: VaultToolError) -> HTTPException:
    detail = str(error)
    if detail.startswith("Unknown"):
        status = 404
    elif "stale" in detail or "must be approved" in detail:
        status = 409
    else:
        status = 422
    return HTTPException(status_code=status, detail=detail)


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


@router.get("/operating-memory")
async def get_operating_memory(
    candidate_limit: int = Query(default=100, ge=1, le=500),
    revision_limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(current_active_user),
):
    """Inspect the current user's active Pi guidance, candidates, and revisions."""

    store = OperatingMemoryStore(current_user.user_id)

    def read_overview() -> (
        tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]
    ):
        return (
            store.read_agents(),
            store.list_candidates(candidate_limit),
            store.list_revisions(revision_limit),
            store.load_state(),
        )

    active_agents, candidates, revisions, state = await _run_blocking(read_overview)
    return {
        "user_id": current_user.user_id,
        "active_agents": active_agents,
        "candidates": candidates,
        "revisions": revisions,
        "optimizer": {
            "last_optimized_at": state.get("last_optimized_at"),
            "processed_trace_count": len(state.get("processed_artifact_hashes") or []),
        },
    }


@router.get("/operating-memory/candidates/{candidate_id}")
async def get_operating_memory_candidate(
    candidate_id: str,
    current_user: User = Depends(current_active_user),
):
    """Read one bounded candidate before an explicit review decision."""

    try:
        store = OperatingMemoryStore(current_user.user_id)
        return await _run_blocking(store.read_candidate, candidate_id)
    except VaultToolError as error:
        raise _operating_memory_http_error(error) from error


@router.post("/operating-memory/candidates/{candidate_id}/review")
async def review_operating_memory_candidate(
    candidate_id: str,
    request: OperatingMemoryReviewRequest,
    current_user: User = Depends(current_active_user),
):
    """Approve or reject a candidate without activating it."""

    try:
        store = OperatingMemoryStore(current_user.user_id)
        message = await _run_blocking(
            store.review_agents_candidate,
            candidate_id,
            decision=request.decision,
            rationale=request.rationale,
            evidence_ids=request.evidence_ids,
        )
    except VaultToolError as error:
        raise _operating_memory_http_error(error) from error
    await record_event(
        severity="info",
        category="memory",
        source="pi_operating_memory",
        title=(
            "Pi operating-memory candidate approved"
            if request.decision == "approve"
            else "Pi operating-memory candidate rejected"
        ),
        detail=message,
        user_id=current_user.user_id,
        metadata={"candidate_id": candidate_id, "decision": request.decision},
    )
    candidate = await _run_blocking(store.read_candidate, candidate_id)
    return {"message": message, "candidate": candidate}


@router.post("/operating-memory/candidates/{candidate_id}/promote")
async def promote_operating_memory_candidate(
    candidate_id: str,
    current_user: User = Depends(current_active_user),
):
    """Activate one reviewed AGENTS.md candidate with stale-base protection."""

    try:
        store = OperatingMemoryStore(current_user.user_id)
        message = await _run_blocking(store.promote_agents_candidate, candidate_id)
    except VaultToolError as error:
        raise _operating_memory_http_error(error) from error
    await record_event(
        severity="info",
        category="memory",
        source="pi_operating_memory",
        title="Pi operating-memory candidate promoted",
        detail=message,
        user_id=current_user.user_id,
        metadata={"candidate_id": candidate_id},
    )
    active_agents = await _run_blocking(store.read_agents)
    return {"message": message, "active_agents": active_agents}


@router.post("/operating-memory/revisions/{revision_id}/rollback")
async def rollback_operating_memory_revision(
    revision_id: str,
    current_user: User = Depends(current_active_user),
):
    """Restore the active guidance that preceded a selected revision."""

    try:
        store = OperatingMemoryStore(current_user.user_id)
        message = await _run_blocking(store.rollback_agents_revision, revision_id)
    except VaultToolError as error:
        raise _operating_memory_http_error(error) from error
    await record_event(
        severity="warning",
        category="memory",
        source="pi_operating_memory",
        title="Pi operating memory rolled back",
        detail=message,
        user_id=current_user.user_id,
        metadata={"revision_id": revision_id},
    )
    active_agents = await _run_blocking(store.read_agents)
    return {"message": message, "active_agents": active_agents}


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
