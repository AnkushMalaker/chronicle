"""Memories a person deliberately saves, and their durable attachments."""

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EnrichmentState(BaseModel):
    state: Literal["pending", "processing", "complete", "failed"] = "pending"
    attempts: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    next_attempt_at: Optional[datetime] = None
    model: Optional[str] = None
    error: Optional[str] = None


class ManualMemoryAttachment(BaseModel):
    attachment_id: str = Field(default_factory=lambda: str(uuid4()))
    media_type: Literal["image"] = "image"
    content_type: str
    original_filename: str
    content_hash: str
    storage_path: str
    byte_size: int
    captured_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    extracted_text: Optional[str] = None
    entities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    sensitive: bool = False
    enrichments: dict[str, EnrichmentState] = Field(
        default_factory=lambda: {
            "description": EnrichmentState(),
            "extracted_text": EnrichmentState(),
            "visual_index": EnrichmentState(),
        }
    )


class ManualMemory(Document):
    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    memory_space_id: Optional[str] = None
    request_id: str
    note: Optional[str] = None
    source: dict[str, Any] = Field(default_factory=dict)
    shared_at: datetime = Field(default_factory=utcnow)
    memory_at: Optional[datetime] = None
    attachments: list[ManualMemoryAttachment]
    vault_path: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "manual_memories"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("memory_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("memory_space_id", ASCENDING)]),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("memory_space_id", ASCENDING),
                    ("request_id", ASCENDING),
                ],
                unique=True,
            ),
            IndexModel([("user_id", ASCENDING), ("shared_at", DESCENDING)]),
            IndexModel(
                [("user_id", ASCENDING), ("attachments.content_hash", ASCENDING)]
            ),
        ]
