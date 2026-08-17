"""Bounded metadata-first discovery of potential Immich memories."""

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import (
    CaptureSource,
    DeviceInputItem,
    utcnow,
)
from advanced_omi_backend.users import User

_DAILY_LIMIT = 12
_BURST_GAP = timedelta(minutes=15)


@dataclass
class _ConversationWindow:
    """A projected conversation, satisfying ``device_context.ConversationWindow``."""

    user_id: str
    conversation_id: str
    created_at: datetime
    audio_total_duration: float | None = None


def _settings() -> tuple[str, str] | None:
    url = os.getenv("IMMICH_URL", "").rstrip("/")
    key = os.getenv("IMMICH_API_KEY", "")
    return (url, key) if url and key else None


async def resolve_immich_user_id() -> str | None:
    """Chronicle user that owns imported Immich references.

    ``IMMICH_USER_ID`` when set; otherwise the admin account, so the wizard can
    configure Immich before the first backend start has minted any ObjectId.
    """
    configured = os.getenv("IMMICH_USER_ID", "")
    if configured:
        return configured
    admin = await User.find_one(User.is_superuser == True)  # noqa: E712
    return str(admin.id) if admin else None


def _asset_time(asset: dict[str, Any]) -> datetime | None:
    raw = asset.get("localDateTime") or asset.get("fileCreatedAt")
    if not raw:
        return None
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Restore UTC stripped by MongoDB before serializing times for Immich."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def select_candidates(
    assets: list[dict[str, Any]], limit: int = _DAILY_LIMIT
) -> list[dict[str, Any]]:
    """Discard screenshots and collapse camera bursts before any thumbnail work."""
    eligible = []
    for asset in assets:
        name = str(asset.get("originalFileName") or "").lower()
        if asset.get("type") not in {None, "IMAGE"} or "screenshot" in name:
            continue
        captured = _asset_time(asset)
        if captured:
            eligible.append((captured, asset))
    eligible.sort(key=lambda pair: pair[0])
    selected: list[dict[str, Any]] = []
    last: datetime | None = None
    for captured, asset in eligible:
        if last is not None and captured - last < _BURST_GAP:
            continue
        selected.append(asset)
        last = captured
        if len(selected) >= limit:
            break
    return selected


async def scan_immich_memories() -> dict[str, Any]:
    settings = _settings()
    if settings is None:
        return {
            "status": "disabled",
            "reason": "IMMICH_URL and IMMICH_API_KEY are required",
        }
    url, key = settings
    user_id = await resolve_immich_user_id()
    if user_id is None:
        return {"status": "disabled", "reason": "no IMMICH_USER_ID and no admin user"}
    source_id = "immich-default"
    source = await CaptureSource.find_one(
        CaptureSource.user_id == user_id, CaptureSource.source_id == source_id
    )
    if source is None:
        source = CaptureSource(
            user_id=user_id,
            source_id=source_id,
            name="Immich",
            provider="immich",
            platform="server",
            token_hash=hashlib.sha256(f"immich:{user_id}".encode()).hexdigest(),
            capabilities=["photos", "thumbnails"],
            status="online",
        )
        await source.insert()

    newest = await DeviceInputItem.find_one(
        DeviceInputItem.user_id == user_id,
        DeviceInputItem.source_id == source_id,
        sort=[("captured_at", -1)],
    )
    newest_at = _as_utc(newest.captured_at) if newest else utcnow() - timedelta(days=2)
    since = newest_at - timedelta(hours=48)
    page: int | None = 1
    assets: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=30, headers={"x-api-key": key, "Accept": "application/json"}
    ) as client:
        while page is not None and len(assets) < 1000:
            response = await client.post(
                f"{url}/api/search/metadata",
                json={
                    "type": "IMAGE",
                    "takenAfter": since.isoformat(),
                    "page": page,
                    "size": 250,
                },
            )
            response.raise_for_status()
            body = response.json().get("assets", {})
            assets.extend(body.get("items", []))
            next_page = body.get("nextPage")
            page = int(next_page) if next_page is not None else None

    accepted = 0
    for asset in select_candidates(assets):
        captured = _asset_time(asset)
        if captured is None:
            continue
        try:
            await DeviceInputItem(
                user_id=user_id,
                source_id=source_id,
                kind="immich_memory",
                source_item_id=str(asset["id"]),
                captured_at=captured,
                metadata={
                    "asset_id": asset["id"],
                    "filename": asset.get("originalFileName"),
                    "review_state": "candidate",
                },
            ).insert()
            accepted += 1
        except DuplicateKeyError:
            pass
    source.status = "online"
    source.last_seen_at = utcnow()
    source.health = {
        "last_scan_candidates": len(assets),
        "last_scan_accepted": accepted,
    }
    await source.save()
    # A daily scan may discover photos after the matching conversation has
    # already completed. Re-run the idempotent bounded linker for recent ones.
    from advanced_omi_backend.services.device_context import (
        request_conversation_context_jobs,
    )

    # The linker needs a window and an owner, not the recordings themselves. This
    # scan covers 48 hours, which is 69 conversations and 63 MB of transcript here;
    # hydrating that on the cron's loop stalled the backend for 1.7 seconds.
    conversations = [
        _ConversationWindow(**row)
        async for row in Conversation.get_pymongo_collection().find(
            {
                "user_id": user_id,
                "created_at": {"$gte": since},
                "deleted": {"$ne": True},
            },
            {
                "_id": 0,
                "user_id": 1,
                "conversation_id": 1,
                "created_at": 1,
                "audio_total_duration": 1,
            },
        )
    ]
    for conversation in conversations:
        await request_conversation_context_jobs(conversation)
    if accepted:
        observations = await DeviceInputItem.find(
            DeviceInputItem.user_id == user_id,
            DeviceInputItem.kind == "observation",
            DeviceInputItem.captured_at >= since - timedelta(minutes=30),
        ).to_list()
        for observation in observations:
            observation.curation = "pending"
            await observation.save()
    return {"status": "ok", "assets": len(assets), "accepted": accepted}
