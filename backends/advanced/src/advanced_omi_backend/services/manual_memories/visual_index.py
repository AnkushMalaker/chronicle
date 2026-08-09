"""Retryable visual indexing for image attachments in manual memories."""

import asyncio
from datetime import timedelta
from typing import Any

from advanced_omi_backend.models.manual_memory import ManualMemory, utcnow
from advanced_omi_backend.services.colpali_client import embed_image, health
from advanced_omi_backend.services.memory.vault_manager import ConvDocVaultManager

_BATCH = 20
_MAX_ATTEMPTS = 8


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2**attempts, 360))


async def process_manual_memory_visual_index() -> dict[str, Any]:
    service = await health()
    if service is None:
        return {"embedded": 0, "failed": 0, "status": "unavailable"}
    model = service.get("model") or "unknown"
    now = utcnow()
    stale_before = now - timedelta(hours=4)
    rows = (
        await ManualMemory.find(
            {
                "attachments": {
                    "$elemMatch": {
                        "media_type": "image",
                        "$or": [
                            {
                                "enrichments.visual_index.state": {
                                    "$in": ["pending", "failed"]
                                }
                            },
                            {
                                "enrichments.visual_index.started_at": {
                                    "$lt": stale_before
                                }
                            },
                        ],
                        "enrichments.visual_index.attempts": {"$lt": _MAX_ATTEMPTS},
                    }
                }
            }
        )
        .sort("-shared_at")
        .limit(_BATCH)
        .to_list()
    )
    embedded = failed = 0
    for memory in rows:
        attachment = next(
            (
                item
                for item in memory.attachments
                if (
                    item.enrichments["visual_index"].state in {"pending", "failed"}
                    or (
                        item.enrichments["visual_index"].started_at is not None
                        and item.enrichments["visual_index"].started_at < stale_before
                    )
                )
                and (
                    item.enrichments["visual_index"].next_attempt_at is None
                    or item.enrichments["visual_index"].next_attempt_at <= now
                )
            ),
            None,
        )
        if attachment is None:
            continue
        state = attachment.enrichments["visual_index"]
        state.state = "processing"
        state.started_at = now
        await memory.save()
        try:
            root = ConvDocVaultManager().user_root(memory.user_id)
            data = await asyncio.to_thread((root / attachment.storage_path).read_bytes)
            await embed_image(
                attachment.attachment_id,
                memory.user_id,
                data,
                attachment.content_type,
                {
                    "memory_id": memory.memory_id,
                    "attachment_id": attachment.attachment_id,
                    "vault_path": memory.vault_path,
                    "shared_at": memory.shared_at.isoformat(),
                    "note": memory.note,
                    "description": attachment.description,
                    "content_hash": attachment.content_hash,
                },
            )
        except Exception as exc:
            state.state = "failed"
            state.attempts += 1
            state.error = str(exc)[:300]
            state.next_attempt_at = now + _backoff(state.attempts)
            failed += 1
        else:
            state.state = "complete"
            state.model = model
            state.completed_at = now
            state.error = None
            state.next_attempt_at = None
            embedded += 1
        memory.updated_at = utcnow()
        await memory.save()
    return {"embedded": embedded, "failed": failed, "model": model}
