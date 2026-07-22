"""Pairing, ingestion, bounded source jobs, and timeline APIs for capture devices."""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import (
    CaptureSource,
    DeviceInputItem,
    DeviceInputJob,
    PairingCode,
    utcnow,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.device_context import request_conversation_context_jobs
from advanced_omi_backend.services.memory.vault_manager import ConvDocVaultManager

router = APIRouter(prefix="/device-input", tags=["device-input"])
_PAIRING_TTL = timedelta(minutes=10)
# Device input is staged in a MongoDB document before it is assembled into a
# Chronicle conversation. Stay below MongoDB's 16 MiB BSON document limit.
_MAX_AUDIO_BYTES = 12 * 1024 * 1024
_ALLOWED_AUDIO_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "video/mp4",  # ScreenPipe audio-only chunks use an MP4 container.
    "audio/ogg",
}
_MAX_IMAGE_BYTES = 25 * 1024 * 1024


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user_id(user: User) -> str:
    return str(user.user_id)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _device_source(authorization: str = Header(default="")) -> CaptureSource:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing device token")
    source = await CaptureSource.find_one(CaptureSource.token_hash == _digest(token))
    if source is None:
        raise HTTPException(status_code=401, detail="Invalid device token")
    return source


class PairRequest(BaseModel):
    code: str
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)
    provider: Literal["screenpipe", "immich"] = "screenpipe"
    capabilities: list[str] = Field(default_factory=list)


class HeartbeatRequest(BaseModel):
    status: Literal["online", "offline", "error"] = "online"
    health: dict[str, Any] = Field(default_factory=dict)


class ActivityItem(BaseModel):
    source_item_id: str
    captured_at: datetime
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivityBatch(BaseModel):
    items: list[ActivityItem] = Field(max_length=1000)


class JobRequest(BaseModel):
    source_id: str
    kind: Literal["screen_context", "thumbnail", "source_media"] = "screen_context"
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    purpose: str
    payload: dict[str, Any] = Field(default_factory=dict)


class JobCompletion(BaseModel):
    success: bool = True
    items: list[ActivityItem] = Field(default_factory=list)
    error: Optional[str] = None


@router.post("/pairing-codes")
async def create_pairing_code(user: User = Depends(current_active_user)):
    raw = secrets.token_urlsafe(9)
    expires_at = utcnow() + _PAIRING_TTL
    await PairingCode(
        user_id=_user_id(user), code_hash=_digest(raw), expires_at=expires_at
    ).insert()
    return {"code": raw, "expires_at": expires_at}


@router.post("/pair")
async def pair_source(body: PairRequest):
    code = await PairingCode.find_one(PairingCode.code_hash == _digest(body.code))
    if code is None or _as_utc(code.expires_at) <= utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired pairing code")
    if body.provider not in {"screenpipe", "immich"}:
        raise HTTPException(status_code=422, detail="Unsupported provider")
    raw_token = secrets.token_urlsafe(32)
    source_id = f"{body.provider}-{secrets.token_hex(8)}"
    source = CaptureSource(
        user_id=code.user_id,
        source_id=source_id,
        name=body.name,
        provider=body.provider,
        platform=body.platform,
        capabilities=body.capabilities,
        token_hash=_digest(raw_token),
        status="online",
        last_seen_at=utcnow(),
    )
    await source.insert()
    await code.delete()
    return {"source_id": source_id, "token": raw_token}


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatRequest, source: CaptureSource = Depends(_device_source)
):
    source.status = (
        body.status if body.status in {"online", "offline", "error"} else "error"
    )
    source.health = body.health
    source.last_seen_at = utcnow()
    await source.save()
    return {"ok": True, "source_id": source.source_id}


@router.post("/activity")
async def ingest_activity(
    body: ActivityBatch, source: CaptureSource = Depends(_device_source)
):
    accepted, duplicates = 0, 0
    for incoming in body.items:
        item = DeviceInputItem(
            user_id=source.user_id,
            source_id=source.source_id,
            kind="activity",
            source_item_id=incoming.source_item_id,
            captured_at=incoming.captured_at,
            ended_at=incoming.ended_at,
            metadata=incoming.metadata,
        )
        try:
            await item.insert()
            accepted += 1
        except DuplicateKeyError:
            duplicates += 1
            existing = await DeviceInputItem.find_one(
                DeviceInputItem.user_id == source.user_id,
                DeviceInputItem.source_id == source.source_id,
                DeviceInputItem.kind == "activity",
                DeviceInputItem.source_item_id == incoming.source_item_id,
            )
            if existing is not None:
                existing.ended_at = incoming.ended_at
                existing.metadata = incoming.metadata
                await existing.save()
    return {"accepted": accepted, "duplicates": duplicates}


@router.post("/audio")
async def ingest_audio(
    file: UploadFile = File(...),
    source_item_id: str = Form(...),
    captured_at: datetime = Form(...),
    duration_seconds: float = Form(..., ge=0),
    device_name: str = Form(...),
    direction: Literal["input", "output", "unknown"] = Form(...),
    content_hash: str = Form(..., pattern=r"^[0-9a-fA-F]{64}$"),
    source: CaptureSource = Depends(_device_source),
):
    existing = await DeviceInputItem.find_one(
        DeviceInputItem.user_id == source.user_id,
        DeviceInputItem.source_id == source.source_id,
        DeviceInputItem.kind == "audio",
        DeviceInputItem.source_item_id == source_item_id,
    )
    if existing:
        return {"status": "duplicate", "item_id": str(existing.id)}
    if file.content_type not in _ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio type")
    data = await file.read(_MAX_AUDIO_BYTES + 1)
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio chunk is too large")
    actual_hash = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_hash, content_hash.lower()):
        raise HTTPException(status_code=422, detail="Content hash mismatch")
    item = DeviceInputItem(
        user_id=source.user_id,
        source_id=source.source_id,
        kind="audio",
        source_item_id=source_item_id,
        captured_at=captured_at,
        ended_at=captured_at + timedelta(seconds=duration_seconds),
        metadata={
            "device_name": device_name,
            "direction": direction,
            "duration_seconds": duration_seconds,
        },
        media_data=data,
        media_filename=file.filename or "chunk.wav",
        media_content_type=file.content_type,
        content_hash=actual_hash,
    )
    try:
        await item.insert()
    except DuplicateKeyError:
        return {"status": "duplicate"}
    return {"status": "accepted", "item_id": str(item.id)}


@router.get("/jobs/next")
async def next_job(source: CaptureSource = Depends(_device_source)):
    await DeviceInputJob.find(
        DeviceInputJob.source_id == source.source_id,
        DeviceInputJob.status == "claimed",
        DeviceInputJob.claimed_at < utcnow() - timedelta(minutes=5),
    ).update_many({"$set": {"status": "pending", "claimed_at": None}})
    job = await DeviceInputJob.find_one(
        DeviceInputJob.source_id == source.source_id,
        DeviceInputJob.status == "pending",
        sort=[("created_at", 1)],
    )
    if job is None:
        return {"job": None}
    job.status = "claimed"
    job.claimed_at = utcnow()
    await job.save()
    return {
        "job": {
            "id": str(job.id),
            "kind": job.kind,
            "start_at": job.start_at,
            "end_at": job.end_at,
            "purpose": job.purpose,
            "payload": job.payload,
        }
    }


@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str, body: JobCompletion, source: CaptureSource = Depends(_device_source)
):
    job = await DeviceInputJob.get(job_id)
    if job is None or job.source_id != source.source_id:
        raise HTTPException(status_code=404, detail="Job not found")
    for incoming in body.items:
        try:
            await DeviceInputItem(
                user_id=source.user_id,
                source_id=source.source_id,
                kind="screen_context",
                source_item_id=incoming.source_item_id,
                captured_at=incoming.captured_at,
                ended_at=incoming.ended_at,
                metadata={
                    **incoming.metadata,
                    "job_id": job_id,
                    "purpose": job.purpose,
                },
                conversation_id=job.payload.get("conversation_id"),
            ).insert()
        except DuplicateKeyError:
            pass
    job.status = "complete" if body.success else "failed"
    job.error = body.error
    job.completed_at = utcnow()
    await job.save()
    return {"ok": True}


@router.get("/sources")
async def list_sources(user: User = Depends(current_active_user)):
    rows = (
        await CaptureSource.find(CaptureSource.user_id == _user_id(user))
        .sort("-last_seen_at")
        .to_list()
    )
    return {
        "sources": [
            {
                "source_id": row.source_id,
                "name": row.name,
                "provider": row.provider,
                "platform": row.platform,
                "status": row.status,
                "health": row.health,
                "last_seen_at": row.last_seen_at,
                "capabilities": row.capabilities,
            }
            for row in rows
        ]
    }


@router.post("/jobs")
async def create_job(body: JobRequest, user: User = Depends(current_active_user)):
    source = await CaptureSource.find_one(
        CaptureSource.user_id == _user_id(user),
        CaptureSource.source_id == body.source_id,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    job = DeviceInputJob(
        user_id=_user_id(user),
        source_id=body.source_id,
        kind=body.kind,
        start_at=body.start_at,
        end_at=body.end_at,
        purpose=body.purpose,
        payload=body.payload,
    )
    await job.insert()
    return {"job_id": str(job.id), "status": job.status}


@router.get("/timeline")
async def timeline(
    start_at: datetime, end_at: datetime, user: User = Depends(current_active_user)
):
    rows = (
        await DeviceInputItem.find(
            DeviceInputItem.user_id == _user_id(user),
            DeviceInputItem.captured_at <= end_at,
            {"$or": [{"ended_at": None}, {"ended_at": {"$gte": start_at}}]},
        )
        .sort("captured_at")
        .to_list()
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "source_id": row.source_id,
                "kind": row.kind,
                "source_item_id": row.source_item_id,
                "captured_at": row.captured_at,
                "ended_at": row.ended_at,
                "metadata": row.metadata,
                "state": row.state,
            }
            for row in rows
        ]
    }


@router.get("/conversations/{conversation_id}/context")
async def conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    rows = (
        await DeviceInputItem.find(
            DeviceInputItem.user_id == _user_id(user),
            DeviceInputItem.conversation_id == conversation_id,
        )
        .sort("captured_at")
        .to_list()
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "source_id": row.source_id,
                "kind": row.kind,
                "captured_at": row.captured_at,
                "ended_at": row.ended_at,
                "metadata": row.metadata,
                "state": row.state,
            }
            for row in rows
        ]
    }


@router.post("/conversations/{conversation_id}/request-context")
async def request_conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    owner = _user_id(user)
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id,
        Conversation.user_id == owner,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    jobs = await request_conversation_context_jobs(conversation)
    return {"jobs": jobs}


@router.delete("/conversations/{conversation_id}/context")
async def clear_conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    result = await DeviceInputItem.find(
        DeviceInputItem.user_id == _user_id(user),
        DeviceInputItem.conversation_id == conversation_id,
        DeviceInputItem.state != "promoted",
    ).update_many({"$set": {"conversation_id": None, "state": "received"}})
    return {"cleared": result.modified_count}


async def _owned_item(item_id: str, user: User) -> DeviceInputItem:
    try:
        item = await DeviceInputItem.get(item_id)
    except Exception:
        item = None
    if item is None or item.user_id != _user_id(user):
        raise HTTPException(status_code=404, detail="Context item not found")
    return item


async def _immich_bytes(asset_id: str, endpoint: str) -> tuple[bytes, str]:
    base = os.getenv("IMMICH_URL", "").rstrip("/")
    key = os.getenv("IMMICH_API_KEY", "")
    if not base or not key:
        raise HTTPException(status_code=503, detail="Immich is not configured")
    async with httpx.AsyncClient(
        timeout=60, headers={"x-api-key": key, "Accept": "image/*"}
    ) as client:
        response = await client.get(f"{base}/api/assets/{asset_id}/{endpoint}")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Source asset is unavailable")
        response.raise_for_status()
        if len(response.content) > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413, detail="Source image exceeds the media limit"
            )
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[
            0
        ]
        if not content_type.startswith("image/"):
            raise HTTPException(
                status_code=415, detail="Source did not return an image"
            )
        return response.content, content_type


@router.get("/items/{item_id}/thumbnail")
async def context_thumbnail(item_id: str, user: User = Depends(current_active_user)):
    item = await _owned_item(item_id, user)
    asset_id = item.metadata.get("asset_id")
    if item.kind != "immich_memory" or not asset_id:
        raise HTTPException(
            status_code=409, detail="This source does not expose an immediate thumbnail"
        )
    data, content_type = await _immich_bytes(str(asset_id), "thumbnail?size=thumbnail")
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/items/{item_id}/promote")
async def promote_context_item(item_id: str, user: User = Depends(current_active_user)):
    item = await _owned_item(item_id, user)
    if item.promoted_path:
        return {"status": "duplicate", "path": item.promoted_path}
    asset_id = item.metadata.get("asset_id")
    if item.kind != "immich_memory" or not asset_id:
        raise HTTPException(
            status_code=409,
            detail="Source-media retrieval is not available for this item",
        )
    data, content_type = await _immich_bytes(str(asset_id), "original")
    digest = hashlib.sha256(data).hexdigest()
    suffixes = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
    }
    suffix = suffixes.get(content_type)
    if suffix is None:
        raise HTTPException(status_code=415, detail="Unsupported vault image type")
    root = ConvDocVaultManager().user_root(_user_id(user))
    media_dir = root / "_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media_path = media_dir / f"{digest}{suffix}"
    if not media_path.exists():
        temporary = media_path.with_suffix(media_path.suffix + ".part")
        temporary.write_bytes(data)
        os.replace(temporary, media_path)
    notes_dir = root / "Media"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_path = notes_dir / f"{digest}.md"
    if not note_path.exists():
        note_tmp = note_path.with_suffix(".md.part")
        note_tmp.write_text(
            f"---\nsource: immich\nasset_id: {asset_id}\ncaptured_at: {item.captured_at.isoformat()}\n---\n\n![[../_media/{media_path.name}]]\n",
            encoding="utf-8",
        )
        os.replace(note_tmp, note_path)
    item.promoted_path = str(media_path.relative_to(root))
    item.state = "promoted"
    await item.save()
    return {
        "status": "promoted",
        "path": item.promoted_path,
        "note": str(note_path.relative_to(root)),
    }
