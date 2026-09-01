"""Deep notification module: intent validation, durable outbox, and Expo adapter."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, Sequence

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ReturnDocument

from advanced_omi_backend.models.notification import (
    NotificationAction,
    NotificationDelivery,
    NotificationIntent,
    NotificationType,
    PushDevice,
    utcnow,
)
from advanced_omi_backend.services.redis_lock import distributed_lock

logger = logging.getLogger(__name__)

AGENT_HOURLY_LIMIT = 6
AGENT_DAILY_LIMIT = 30
DEDUPE_WINDOW = timedelta(minutes=15)
DEFAULT_EXPIRY = timedelta(hours=24)
RECEIPT_DELAY = timedelta(minutes=15)
ALLOWED_CHRONICLE_ROUTES = {"timeline", "settings", "memory_ledger"}
TERMINAL_DELIVERY_STATES = {"provider_accepted", "failed", "expired"}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class NotificationCommand(BaseModel):
    notification_type: NotificationType
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=500)
    action: NotificationAction = "none"
    route: str | None = None
    route_params: dict[str, str] = Field(default_factory=dict)
    deliver_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    dedupe_key: str | None = Field(default=None, max_length=160)

    @field_validator("title", "body")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("notification text cannot be blank")
        return stripped

    @field_validator("route_params")
    @classmethod
    def _bounded_route_params(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 8:
            raise ValueError("at most 8 route parameters are allowed")
        if any(len(key) > 40 or len(item) > 160 for key, item in value.items()):
            raise ValueError("route parameter is too long")
        return value

    @model_validator(mode="after")
    def _validate_action(self) -> "NotificationCommand":
        if self.action == "open_chronicle_route":
            if self.route not in ALLOWED_CHRONICLE_ROUTES:
                raise ValueError("Chronicle route is not allowlisted")
        elif self.route is not None or self.route_params:
            raise ValueError("route data requires open_chronicle_route")
        self.deliver_at = _as_utc(self.deliver_at)
        self.expires_at = _as_utc(self.expires_at or self.deliver_at + DEFAULT_EXPIRY)
        if self.expires_at <= self.deliver_at:
            raise ValueError("expires_at must be after deliver_at")
        return self


class NotificationQuotaExceeded(ValueError):
    pass


class TransientPushError(RuntimeError):
    pass


@dataclass(frozen=True)
class PushMessage:
    token: str
    title: str
    body: str
    data: dict[str, Any]
    notification_type: NotificationType
    ttl_seconds: int


@dataclass(frozen=True)
class PushTicket:
    token: str
    status: str
    ticket_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None


class PushProvider(Protocol):
    async def send(self, messages: Sequence[PushMessage]) -> list[PushTicket]: ...

    async def receipts(
        self, ticket_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...


class ExpoPushProvider:
    """True-external adapter for Expo's push tickets and receipts."""

    send_url = "https://exp.host/--/api/v2/push/send"
    receipts_url = "https://exp.host/--/api/v2/push/getReceipts"

    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token or os.getenv("EXPO_ACCESS_TOKEN", "")
        if not self.access_token:
            raise RuntimeError(
                "EXPO_ACCESS_TOKEN is required; enable Expo push security and configure the backend secret"
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    async def send(self, messages: Sequence[PushMessage]) -> list[PushTicket]:
        if not messages:
            return []
        payload = [
            {
                "to": message.token,
                "title": message.title,
                "body": message.body,
                "data": message.data,
                "sound": "default" if message.notification_type == "priority" else None,
                "priority": (
                    "high" if message.notification_type == "priority" else "default"
                ),
                "channelId": message.notification_type,
                "categoryId": message.notification_type,
                "ttl": message.ttl_seconds,
            }
            for message in messages
        ]
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    self.send_url, headers=self._headers(), json=payload
                )
        except httpx.HTTPError as error:
            raise TransientPushError(f"Expo send failed: {error}") from error
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientPushError(f"Expo send returned {response.status_code}")
        response.raise_for_status()
        rows = response.json().get("data", [])
        tickets: list[PushTicket] = []
        for message, row in zip(messages, rows, strict=False):
            details = row.get("details") or {}
            tickets.append(
                PushTicket(
                    token=message.token,
                    status=str(row.get("status") or "error"),
                    ticket_id=row.get("id"),
                    error=details.get("error") or row.get("message"),
                    raw=row,
                )
            )
        if len(tickets) < len(messages):
            tickets.extend(
                PushTicket(
                    token=item.token, status="error", error="missing Expo ticket"
                )
                for item in messages[len(tickets) :]
            )
        return tickets

    async def receipts(self, ticket_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not ticket_ids:
            return {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    self.receipts_url,
                    headers=self._headers(),
                    json={"ids": list(ticket_ids)},
                )
        except httpx.HTTPError as error:
            raise TransientPushError(f"Expo receipt lookup failed: {error}") from error
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientPushError(
                f"Expo receipt lookup returned {response.status_code}"
            )
        response.raise_for_status()
        return response.json().get("data", {})


async def register_push_device(
    *,
    user_id: str,
    installation_id: str,
    expo_push_token: str,
    platform: str,
    app_version: str | None = None,
    build_version: str | None = None,
) -> PushDevice:
    """Upsert one installation and atomically transfer a reused token to its owner."""

    if platform not in {"ios", "android"}:
        raise ValueError("push platform must be ios or android")
    if not expo_push_token.startswith(("ExponentPushToken[", "ExpoPushToken[")):
        raise ValueError("invalid Expo push token")
    now = utcnow()
    token_owner = await PushDevice.find_one(
        PushDevice.expo_push_token == expo_push_token
    )
    installation = await PushDevice.find_one(
        PushDevice.user_id == user_id,
        PushDevice.installation_id == installation_id,
    )
    if token_owner is not None and token_owner.id != getattr(installation, "id", None):
        # Keep the token's unique document alive and move it with one conditional
        # update. A separate destination document cannot be folded into this transfer
        # atomically on standalone Mongo, so reject that inconsistent installation
        # state without deleting either registration.
        if installation is not None:
            raise ValueError(
                "installation is already registered with a different push token"
            )
        transferred = await PushDevice.get_pymongo_collection().find_one_and_update(
            {"_id": token_owner.id, "expo_push_token": expo_push_token},
            {
                "$set": {
                    "user_id": user_id,
                    "installation_id": installation_id,
                    "platform": platform,
                    "app_version": app_version,
                    "build_version": build_version,
                    "enabled": True,
                    "disabled_at": None,
                    "disabled_reason": None,
                    "last_registered_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if transferred is None:
            raise RuntimeError("push token ownership changed during registration")
        return PushDevice.model_validate(transferred)
    if installation is None:
        installation = PushDevice(
            user_id=user_id,
            installation_id=installation_id,
            expo_push_token=expo_push_token,
            platform=platform,
            app_version=app_version,
            build_version=build_version,
        )
        await installation.insert()
        return installation
    installation.expo_push_token = expo_push_token
    installation.platform = platform
    installation.app_version = app_version
    installation.build_version = build_version
    installation.enabled = True
    installation.disabled_at = None
    installation.disabled_reason = None
    installation.last_registered_at = now
    installation.updated_at = now
    await installation.save()
    return installation


async def unregister_push_device(*, user_id: str, installation_id: str) -> bool:
    device = await PushDevice.find_one(
        PushDevice.user_id == user_id,
        PushDevice.installation_id == installation_id,
    )
    if device is None:
        return False
    await device.delete()
    return True


async def _enforce_agent_quota(user_id: str, now: datetime) -> None:
    collection = NotificationIntent.get_pymongo_collection()
    base = {"user_id": user_id, "source": "agent", "state": {"$ne": "suppressed"}}
    hourly, daily = await asyncio.gather(
        collection.count_documents(
            {**base, "created_at": {"$gte": now - timedelta(hours=1)}}
        ),
        collection.count_documents(
            {**base, "created_at": {"$gte": now - timedelta(days=1)}}
        ),
    )
    if hourly >= AGENT_HOURLY_LIMIT or daily >= AGENT_DAILY_LIMIT:
        raise NotificationQuotaExceeded(
            f"agent notification quota exceeded ({AGENT_HOURLY_LIMIT}/hour, {AGENT_DAILY_LIMIT}/day)"
        )


async def enqueue_notification(
    *,
    user_id: str,
    command: NotificationCommand,
    source: str,
    actor_id: str | None = None,
    queue_immediately: bool = True,
) -> tuple[NotificationIntent, bool]:
    """Persist one validated intent, returning ``(intent, created)``."""

    claim_parts: tuple[str, ...] | None = None
    if source == "agent":
        # Serialize quota counting and insertion for one owner.
        claim_parts = ("agent-quota", user_id)
    elif command.dedupe_key:
        claim_parts = ("dedupe", user_id, source, command.dedupe_key)
    if claim_parts is None:
        return await _enqueue_notification_locked(
            user_id=user_id,
            command=command,
            source=source,
            actor_id=actor_id,
            queue_immediately=queue_immediately,
        )
    claim = hashlib.sha256("\0".join(claim_parts).encode()).hexdigest()
    async with distributed_lock(f"notifications:intent:{claim}"):
        return await _enqueue_notification_locked(
            user_id=user_id,
            command=command,
            source=source,
            actor_id=actor_id,
            queue_immediately=queue_immediately,
        )


async def _enqueue_notification_locked(
    *,
    user_id: str,
    command: NotificationCommand,
    source: str,
    actor_id: str | None = None,
    queue_immediately: bool = True,
) -> tuple[NotificationIntent, bool]:
    """Count, deduplicate, and insert while the relevant claim is held."""

    now = utcnow()
    if source == "agent":
        await _enforce_agent_quota(user_id, now)
    if command.dedupe_key:
        existing = (
            await NotificationIntent.find(
                {
                    "user_id": user_id,
                    "source": source,
                    "dedupe_key": command.dedupe_key,
                    "created_at": {"$gte": now - DEDUPE_WINDOW},
                    "state": {"$nin": ["failed", "expired"]},
                }
            )
            .sort("-created_at")
            .first_or_none()
        )
        if existing is not None:
            return existing, False
    intent = NotificationIntent(
        user_id=user_id,
        notification_type=command.notification_type,
        title=command.title,
        body=command.body,
        action=command.action,
        route=command.route,
        route_params=command.route_params,
        deliver_at=command.deliver_at,
        expires_at=command.expires_at,
        dedupe_key=command.dedupe_key,
        source=source,
        actor_id=actor_id,
    )
    await intent.insert()
    if queue_immediately and intent.deliver_at <= now:
        job_id = await asyncio.to_thread(_queue_dispatch, intent.notification_id)
        if job_id:
            intent.state = "queued"
            intent.queued_at = now
            intent.updated_at = now
            await intent.save()
    return intent, True


def _queue_dispatch(notification_id: str) -> str | None:
    # Lazy to keep the service independent of queue-controller worker imports.
    from advanced_omi_backend.controllers.queue_controller import (
        enqueue_notification_dispatch,
    )

    return enqueue_notification_dispatch(notification_id)


def _message_data(intent: NotificationIntent) -> dict[str, Any]:
    return {
        "notification_id": intent.notification_id,
        "type": intent.notification_type,
        "action": intent.action,
        **(
            {"route": intent.route, "route_params": intent.route_params}
            if intent.route
            else {}
        ),
    }


async def dispatch_notification(
    notification_id: str, provider: PushProvider | None = None
) -> dict[str, Any]:
    """Expand one intent to devices and submit through the injected provider."""

    provider = provider or ExpoPushProvider()
    intent = await NotificationIntent.find_one(
        NotificationIntent.notification_id == notification_id
    )
    if intent is None:
        return {"notification_id": notification_id, "state": "missing"}
    now = utcnow()
    if _as_utc(intent.expires_at) <= now:
        intent.state = "expired"
        intent.completed_at = now
        intent.updated_at = now
        await intent.save()
        return {"notification_id": notification_id, "state": "expired"}
    devices = await PushDevice.find(
        PushDevice.user_id == intent.user_id,
        PushDevice.enabled == True,  # noqa: E712
    ).to_list()
    if not devices:
        intent.state = "suppressed"
        intent.last_error = "no registered push devices"
        intent.completed_at = now
        intent.updated_at = now
        await intent.save()
        return {"notification_id": notification_id, "state": "suppressed", "devices": 0}

    deliveries: list[NotificationDelivery] = []
    messages: list[PushMessage] = []
    for device in devices:
        delivery = await NotificationDelivery.find_one(
            NotificationDelivery.notification_id == notification_id,
            NotificationDelivery.installation_id == device.installation_id,
        )
        if delivery is not None and delivery.state in {
            "submitted",
            "provider_accepted",
        }:
            continue
        if delivery is None:
            delivery = NotificationDelivery(
                notification_id=notification_id,
                user_id=intent.user_id,
                installation_id=device.installation_id,
                expo_push_token=device.expo_push_token,
            )
            await delivery.insert()
        deliveries.append(delivery)
        messages.append(
            PushMessage(
                token=device.expo_push_token,
                title=intent.title,
                body=intent.body,
                data=_message_data(intent),
                notification_type=intent.notification_type,
                ttl_seconds=max(
                    1, int((_as_utc(intent.expires_at) - now).total_seconds())
                ),
            )
        )
    if not messages:
        return {
            "notification_id": notification_id,
            "state": intent.state,
            "devices": len(devices),
        }

    tickets = await provider.send(messages)
    submitted = 0
    for delivery, ticket in zip(deliveries, tickets, strict=True):
        delivery.attempts += 1
        delivery.updated_at = now
        delivery.provider_response = ticket.raw or {}
        if ticket.status == "ok" and ticket.ticket_id:
            delivery.state = "submitted"
            delivery.provider_ticket_id = ticket.ticket_id
            delivery.submitted_at = now
            delivery.receipt_due_at = now + RECEIPT_DELAY
            delivery.last_error = None
            submitted += 1
        else:
            delivery.state = "failed"
            delivery.last_error = ticket.error or "Expo rejected notification"
            if ticket.error == "DeviceNotRegistered":
                await _disable_token(delivery.expo_push_token, ticket.error)
        await delivery.save()

    intent.state = "submitted" if submitted else "failed"
    intent.submitted_at = now if submitted else None
    intent.completed_at = None if submitted else now
    intent.last_error = None if submitted else "all provider submissions failed"
    intent.updated_at = now
    await intent.save()
    return {
        "notification_id": notification_id,
        "state": intent.state,
        "devices": len(devices),
        "submitted": submitted,
    }


async def _disable_token(token: str, reason: str) -> None:
    device = await PushDevice.find_one(PushDevice.expo_push_token == token)
    if device is None:
        return
    now = utcnow()
    device.enabled = False
    device.disabled_at = now
    device.disabled_reason = reason
    device.updated_at = now
    await device.save()


async def check_due_receipts(provider: PushProvider | None = None) -> dict[str, int]:
    """Resolve provider receipts that are old enough to exist, never claiming device delivery."""

    provider = provider or ExpoPushProvider()
    now = utcnow()
    deliveries = (
        await NotificationDelivery.find(
            NotificationDelivery.state == "submitted",
            NotificationDelivery.receipt_due_at <= now,
        )
        .limit(1000)
        .to_list()
    )
    ticket_ids = [
        item.provider_ticket_id for item in deliveries if item.provider_ticket_id
    ]
    receipts = await provider.receipts(ticket_ids)
    intent_ids = {item.notification_id for item in deliveries}
    intents = await NotificationIntent.find(
        {"notification_id": {"$in": list(intent_ids)}}
    ).to_list()
    intents_by_id = {item.notification_id: item for item in intents}
    touched_intents: set[str] = set()
    accepted = failed = 0
    for delivery in deliveries:
        if not delivery.provider_ticket_id:
            continue
        receipt = receipts.get(delivery.provider_ticket_id)
        if receipt is None:
            intent = intents_by_id.get(delivery.notification_id)
            if intent is not None and _as_utc(intent.expires_at) <= now:
                touched_intents.add(delivery.notification_id)
                delivery.state = "failed"
                delivery.receipt_checked_at = now
                delivery.updated_at = now
                delivery.last_error = "provider receipt unavailable before expiry"
                await delivery.save()
                failed += 1
            continue
        touched_intents.add(delivery.notification_id)
        delivery.receipt_checked_at = now
        delivery.updated_at = now
        delivery.provider_response = receipt
        if receipt.get("status") == "ok":
            delivery.state = "provider_accepted"
            delivery.last_error = None
            accepted += 1
        else:
            details = receipt.get("details") or {}
            error = (
                details.get("error")
                or receipt.get("message")
                or "provider rejected notification"
            )
            delivery.state = "failed"
            delivery.last_error = str(error)
            failed += 1
            if error == "DeviceNotRegistered":
                await _disable_token(delivery.expo_push_token, str(error))
        await delivery.save()
    for notification_id in touched_intents:
        await _refresh_intent_state(notification_id, now)
    return {"checked": len(deliveries), "accepted": accepted, "failed": failed}


async def _refresh_intent_state(notification_id: str, now: datetime) -> None:
    intent = await NotificationIntent.find_one(
        NotificationIntent.notification_id == notification_id
    )
    if intent is None:
        return
    deliveries = await NotificationDelivery.find(
        NotificationDelivery.notification_id == notification_id
    ).to_list()
    if any(item.state == "provider_accepted" for item in deliveries):
        intent.state = "provider_accepted"
        intent.completed_at = now
        intent.last_error = None
    elif deliveries and all(
        item.state in TERMINAL_DELIVERY_STATES for item in deliveries
    ):
        intent.state = "failed"
        intent.completed_at = now
        intent.last_error = "all provider deliveries failed"
    intent.updated_at = now
    await intent.save()


async def queue_due_notifications() -> dict[str, int]:
    """Cron entrypoint: perform only bounded Mongo work and RQ enqueues."""

    now = utcnow()
    collection = NotificationIntent.get_pymongo_collection()
    expired = await collection.update_many(
        {"state": "pending", "expires_at": {"$lte": now}},
        {"$set": {"state": "expired", "completed_at": now, "updated_at": now}},
    )
    due = (
        await NotificationIntent.find(
            NotificationIntent.state == "pending",
            NotificationIntent.deliver_at <= now,
            NotificationIntent.expires_at > now,
        )
        .sort("+deliver_at")
        .limit(100)
        .to_list()
    )
    queued = 0
    for intent in due:
        if await asyncio.to_thread(_queue_dispatch, intent.notification_id):
            intent.state = "queued"
            intent.queued_at = now
            intent.updated_at = now
            await intent.save()
            queued += 1
    return {"due": len(due), "queued": queued, "expired": int(expired.modified_count)}


async def queue_receipt_check() -> dict[str, int | str | None]:
    """Cron entrypoint: enqueue one receipt worker only when receipts are due."""

    due = await NotificationDelivery.find_one(
        NotificationDelivery.state == "submitted",
        NotificationDelivery.receipt_due_at <= utcnow(),
    )
    if due is None:
        return {"due": 0, "job_id": None}
    # Lazy to keep the cron service independent of queue-controller worker imports.
    from advanced_omi_backend.controllers.queue_controller import (
        enqueue_notification_receipts,
    )

    job_id = await asyncio.to_thread(enqueue_notification_receipts)
    return {"due": 1, "job_id": job_id}
