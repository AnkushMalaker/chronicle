"""Durable records for external multimodal capture sources."""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CaptureSource(Document):
    user_id: str
    source_id: str
    name: str
    provider: Literal["screenpipe", "immich"]
    platform: str
    token_hash: str
    capabilities: list[str] = Field(default_factory=list)
    status: Literal["pairing", "online", "offline", "error"] = "pairing"
    health: dict[str, Any] = Field(default_factory=dict)
    last_seen_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "capture_sources"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("source_id", ASCENDING)], unique=True),
            IndexModel([("token_hash", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("last_seen_at", DESCENDING)]),
        ]


class PairingCode(Document):
    user_id: str
    code_hash: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "device_input_pairing_codes"
        indexes = [
            IndexModel([("code_hash", ASCENDING)], unique=True),
            IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
        ]


class DeviceInputItem(Document):
    user_id: str
    source_id: str
    kind: Literal["audio", "activity", "screen_context", "immich_memory"]
    source_item_id: str
    captured_at: datetime
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    media_data: Optional[bytes] = None
    media_filename: Optional[str] = None
    media_content_type: Optional[str] = None
    content_hash: Optional[str] = None
    conversation_id: Optional[str] = None
    promoted_path: Optional[str] = None
    state: Literal["received", "linked", "promoted", "rejected"] = "received"
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "device_input_items"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("source_id", ASCENDING),
                    ("kind", ASCENDING),
                    ("source_item_id", ASCENDING),
                ],
                unique=True,
            ),
            IndexModel([("user_id", ASCENDING), ("captured_at", DESCENDING)]),
            IndexModel([("conversation_id", ASCENDING), ("captured_at", ASCENDING)]),
        ]


class DeviceInputJob(Document):
    user_id: str
    source_id: str
    kind: Literal["screen_context", "thumbnail", "source_media"]
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    purpose: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "claimed", "complete", "failed"] = "pending"
    claimed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "device_input_jobs"
        indexes = [
            IndexModel(
                [
                    ("source_id", ASCENDING),
                    ("status", ASCENDING),
                    ("created_at", ASCENDING),
                ]
            ),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
        ]
