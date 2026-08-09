"""Creation and browsing APIs for memories a person deliberately saves."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import NAMESPACE_URL, uuid5

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.manual_memory import (
    ManualMemory,
    ManualMemoryAttachment,
    utcnow,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.manual_memories.image import write_memory_note
from advanced_omi_backend.services.memory.vault_manager import ConvDocVaultManager
from advanced_omi_backend.services.memory.vault_media import (
    promote_image_bytes,
    sniff_image_type,
)
from advanced_omi_backend.workers.manual_memory_jobs import enqueue_manual_memory_image

router = APIRouter(prefix="/manual-memories", tags=["manual-memories"])
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 8
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _user_id(user: User) -> str:
    return str(user.user_id)


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _serialize(memory: ManualMemory) -> dict:
    return {
        "memory_id": memory.memory_id,
        "note": memory.note,
        "source": memory.source,
        "shared_at": memory.shared_at,
        "memory_at": memory.memory_at,
        "vault_path": memory.vault_path,
        "attachments": [
            attachment.model_dump(mode="json") for attachment in memory.attachments
        ],
    }


async def _owned_memory(memory_id: str, user: User) -> ManualMemory:
    memory = await ManualMemory.find_one(
        ManualMemory.user_id == _user_id(user), ManualMemory.memory_id == memory_id
    )
    if memory is None:
        raise HTTPException(status_code=404, detail="Manual memory not found")
    return memory


def _owned_attachment(
    memory: ManualMemory, attachment_id: str
) -> ManualMemoryAttachment:
    attachment = next(
        (item for item in memory.attachments if item.attachment_id == attachment_id),
        None,
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return attachment


@router.post("", status_code=202)
async def create_manual_memory(
    attachments: list[UploadFile] = File(...),
    request_id: str = Form(..., min_length=8, max_length=128),
    note: Optional[str] = Form(default=None),
    source_application: Optional[str] = Form(default=None),
    memory_at: Optional[datetime] = Form(default=None),
    user: User = Depends(current_active_user),
):
    """Durably create a manual memory before scheduling optional enrichment."""

    user_id = _user_id(user)
    existing = await ManualMemory.find_one(
        ManualMemory.user_id == user_id, ManualMemory.request_id == request_id
    )
    if existing is not None:
        return {"status": "existing", **_serialize(existing)}
    if not attachments or len(attachments) > MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=422, detail=f"Provide 1–{MAX_ATTACHMENTS} attachments"
        )
    cleaned_note = (note or "").strip() or None
    if cleaned_note and len(cleaned_note) > 2000:
        raise HTTPException(status_code=422, detail="Note exceeds 2000 characters")

    root = ConvDocVaultManager().user_root(user_id)
    stored: list[ManualMemoryAttachment] = []
    for upload in attachments:
        declared = (upload.content_type or "").split(";", 1)[0].lower()
        data = await upload.read(MAX_ATTACHMENT_BYTES + 1)
        if not data:
            raise HTTPException(status_code=400, detail="Empty attachment")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="Attachment exceeds 10 MiB")
        detected = sniff_image_type(data)
        if declared not in ALLOWED_IMAGE_TYPES or detected != declared:
            raise HTTPException(
                status_code=415,
                detail="Attachments must be valid JPEG, PNG, or WebP images",
            )
        storage_path, digest = await asyncio.to_thread(
            promote_image_bytes, data, detected, root
        )
        stored.append(
            ManualMemoryAttachment(
                content_type=detected,
                original_filename=Path(upload.filename or f"image-{digest[:12]}").name,
                content_hash=digest,
                storage_path=storage_path,
                byte_size=len(data),
            )
        )

    memory = ManualMemory(
        memory_id=str(uuid5(NAMESPACE_URL, f"chronicle:{user_id}:{request_id}")),
        user_id=user_id,
        request_id=request_id,
        note=cleaned_note,
        source={
            "kind": "share_sheet",
            "application": (source_application or "").strip() or None,
        },
        shared_at=utcnow(),
        memory_at=_utc(memory_at) if memory_at else None,
        attachments=stored,
        vault_path="",
    )
    memory.vault_path = await asyncio.to_thread(write_memory_note, memory, root)
    try:
        await memory.insert()
    except DuplicateKeyError:
        existing = await ManualMemory.find_one(
            ManualMemory.user_id == user_id, ManualMemory.request_id == request_id
        )
        if existing is None:
            raise
        return {"status": "existing", **_serialize(existing)}
    for attachment in memory.attachments:
        enqueue_manual_memory_image(memory.memory_id, attachment.attachment_id)
    return {"status": "created", **_serialize(memory)}


@router.get("")
async def list_manual_memories(
    limit: int = Query(default=50, ge=1, le=100),
    before: Optional[datetime] = None,
    user: User = Depends(current_active_user),
):
    query: dict = {"user_id": _user_id(user)}
    if before:
        query["shared_at"] = {"$lt": _utc(before)}
    rows = await ManualMemory.find(query).sort("-shared_at").limit(limit + 1).to_list()
    page = rows[:limit]
    return {
        "items": [_serialize(row) for row in page],
        "next_before": page[-1].shared_at if len(rows) > limit else None,
    }


@router.get("/{memory_id}")
async def get_manual_memory(memory_id: str, user: User = Depends(current_active_user)):
    return _serialize(await _owned_memory(memory_id, user))


async def _attachment_response(memory: ManualMemory, attachment_id: str) -> Response:
    attachment = _owned_attachment(memory, attachment_id)
    root = ConvDocVaultManager().user_root(memory.user_id).resolve()
    path = (root / attachment.storage_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Attachment content not found")
    return Response(
        content=await asyncio.to_thread(path.read_bytes),
        media_type=attachment.content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/{memory_id}/attachments/{attachment_id}/content")
async def attachment_content(
    memory_id: str, attachment_id: str, user: User = Depends(current_active_user)
):
    return await _attachment_response(
        await _owned_memory(memory_id, user), attachment_id
    )


@router.get("/{memory_id}/attachments/{attachment_id}/thumbnail")
async def attachment_thumbnail(
    memory_id: str, attachment_id: str, user: User = Depends(current_active_user)
):
    return await _attachment_response(
        await _owned_memory(memory_id, user), attachment_id
    )
