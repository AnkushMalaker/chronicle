"""Persistent isolated memory spaces and their publication ledger."""

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SeededVaultNote(BaseModel):
    note_path: str
    content_hash: str
    byte_size: int


class SpaceSourceRef(BaseModel):
    kind: Literal["conversation", "chat", "manual", "obsidian"]
    source_id: str


class SpaceValidationFinding(BaseModel):
    rule: str
    detail: str
    severity: Literal["semantic", "conflict"]


class SpaceMergeChange(BaseModel):
    change_id: str = Field(default_factory=lambda: str(uuid4()))
    note_path: str
    operation: Literal["create", "update", "delete"]
    before_hash: Optional[str] = None
    before_text: Optional[str] = None
    after_text: Optional[str] = None
    conflict: Optional[str] = None
    source_refs: list[SpaceSourceRef] = Field(default_factory=list)
    validation_findings: list[SpaceValidationFinding] = Field(default_factory=list)


class MemorySpace(Document):
    """A named, user-owned vault isolated from Main."""

    space_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    name: str = Field(min_length=1, max_length=120)
    state: Literal["active", "merging", "archived"] = "active"
    seed_notes: list[SeededVaultNote] = Field(default_factory=list)
    sync_state: Literal["unpaired", "syncing", "healthy", "frozen", "error"] = (
        "unpaired"
    )
    sync_error: Optional[str] = None
    merge_checkpoint: Optional[str] = None
    active_merge_proposal_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    archived_at: Optional[datetime] = None

    class Settings:
        name = "memory_spaces"
        indexes = [
            IndexModel([("space_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("state", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("updated_at", DESCENDING)]),
        ]


class SpaceMergeProposal(Document):
    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    space_id: str
    state: Literal[
        "generating",
        "pending",
        "applying",
        "applied",
        "cancelled",
        "stale",
        "failed",
    ] = "generating"
    changes: list[SpaceMergeChange] = Field(default_factory=list)
    accepted_change_ids: list[str] = Field(default_factory=list)
    rejected_change_ids: list[str] = Field(default_factory=list)
    deferred_event_count: int = 0
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    generated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    class Settings:
        name = "space_merge_proposals"
        indexes = [
            IndexModel([("proposal_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("space_id", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("state", ASCENDING)]),
        ]


class DeferredSpaceEvent(Document):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    space_id: str
    source_kind: Literal["conversation", "chat"]
    source_id: str
    event_type: str
    idempotency_key: str
    causal_order: int
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    state: Literal["pending", "dispatching", "dispatched", "failed"] = "pending"
    attempts: int = 0
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    dispatched_at: Optional[datetime] = None

    class Settings:
        name = "deferred_space_events"
        indexes = [
            IndexModel([("event_id", ASCENDING)], unique=True),
            IndexModel([("idempotency_key", ASCENDING)], unique=True),
            IndexModel(
                [
                    ("space_id", ASCENDING),
                    ("source_kind", ASCENDING),
                    ("source_id", ASCENDING),
                    ("event_type", ASCENDING),
                ],
                unique=True,
                name="space_event_idempotency",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("space_id", ASCENDING), ("state", ASCENDING)]
            ),
        ]
