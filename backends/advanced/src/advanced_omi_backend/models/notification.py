"""Durable push-notification intents, devices, and provider deliveries."""

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


NotificationType = Literal["priority", "agent"]
NotificationAction = Literal["none", "open_immich", "open_chronicle_route"]
NotificationIntentState = Literal[
    "pending",
    "queued",
    "submitted",
    "provider_accepted",
    "failed",
    "expired",
    "suppressed",
]
NotificationDeliveryState = Literal[
    "pending",
    "submitted",
    "provider_accepted",
    "failed",
    "expired",
]


class PushDevice(Document):
    """One Chronicle installation that may receive Expo pushes for one owner."""

    user_id: str
    installation_id: str
    expo_push_token: str
    platform: Literal["ios", "android"]
    app_version: Optional[str] = None
    build_version: Optional[str] = None
    enabled: bool = True
    last_registered_at: datetime = Field(default_factory=utcnow)
    disabled_at: Optional[datetime] = None
    disabled_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "push_devices"
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("installation_id", ASCENDING)],
                unique=True,
                name="push_device_owner_installation",
            ),
            IndexModel(
                [("expo_push_token", ASCENDING)],
                unique=True,
                name="push_device_expo_token",
            ),
            IndexModel([("user_id", ASCENDING), ("enabled", ASCENDING)]),
        ]


class NotificationIntent(Document):
    """Owner-scoped notification request; provider mechanics stay behind the module."""

    notification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    notification_type: NotificationType
    title: str
    body: str
    action: NotificationAction = "none"
    route: Optional[str] = None
    route_params: dict[str, str] = Field(default_factory=dict)
    deliver_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    dedupe_key: Optional[str] = None
    source: str
    actor_id: Optional[str] = None
    state: NotificationIntentState = "pending"
    last_error: Optional[str] = None
    queued_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "notification_intents"
        indexes = [
            IndexModel([("notification_id", ASCENDING)], unique=True),
            IndexModel(
                [("state", ASCENDING), ("deliver_at", ASCENDING)],
                name="notification_due",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="notification_owner_history",
            ),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("source", ASCENDING),
                    ("dedupe_key", ASCENDING),
                    ("created_at", DESCENDING),
                ],
                name="notification_dedupe_lookup",
            ),
        ]


class NotificationDelivery(Document):
    """One provider submission for one intent/device pair."""

    delivery_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    notification_id: str
    user_id: str
    installation_id: str
    expo_push_token: str
    state: NotificationDeliveryState = "pending"
    provider_ticket_id: Optional[str] = None
    provider_response: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    submitted_at: Optional[datetime] = None
    receipt_due_at: Optional[datetime] = None
    receipt_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "notification_deliveries"
        indexes = [
            IndexModel([("delivery_id", ASCENDING)], unique=True),
            IndexModel(
                [("notification_id", ASCENDING), ("installation_id", ASCENDING)],
                unique=True,
                name="notification_delivery_once_per_device",
            ),
            IndexModel(
                [("state", ASCENDING), ("receipt_due_at", ASCENDING)],
                name="notification_receipts_due",
            ),
        ]
