"""Image-specific enrichment for manual-memory attachments."""

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from advanced_omi_backend.models.manual_memory import ManualMemory, utcnow
from advanced_omi_backend.services.memory.scope import MemoryScope, MemoryScopeResolver
from advanced_omi_backend.services.memory.vault_lock import vault_note_lock
from advanced_omi_backend.services.memory.vault_media import write_manual_memory_note
from advanced_omi_backend.services.vision import (
    VisionError,
    VisionUnavailable,
    run_structured_vision,
)

from .config import MAX_IMAGE_ANALYSIS_ATTEMPTS, manual_memory_settings

logger = logging.getLogger(__name__)
_BATCH = 8
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": "string"},
        "ocr_text": {"type": "string"},
        "app_or_site": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "sensitive": {"type": "boolean"},
    },
    "required": [
        "description",
        "ocr_text",
        "app_or_site",
        "entities",
        "tags",
        "sensitive",
    ],
}
_PROMPT = """Describe this deliberately saved image so it can be found months later.
Say what it shows and what it appears useful for. Transcribe readable text verbatim.
The user's note is authoritative context; do not replace or contradict it."""


def _attachment(memory: ManualMemory, attachment_id: str):
    return next(
        (item for item in memory.attachments if item.attachment_id == attachment_id),
        None,
    )


async def _claim(
    memory_id: str, attachment_id: str, timeout: int
) -> Optional[ManualMemory]:
    now = utcnow()
    stale_before = now - timedelta(seconds=2 * timeout)
    result = await ManualMemory.get_pymongo_collection().update_one(
        {
            "memory_id": memory_id,
            "attachments": {
                "$elemMatch": {
                    "attachment_id": attachment_id,
                    "$or": [
                        {
                            "enrichments.description.state": {
                                "$in": ["pending", "failed"]
                            }
                        },
                        {"enrichments.description.started_at": {"$lt": stale_before}},
                    ],
                }
            },
        },
        {
            "$set": {
                "attachments.$[a].enrichments.description.state": "processing",
                "attachments.$[a].enrichments.description.started_at": now,
                "attachments.$[a].enrichments.extracted_text.state": "processing",
                "attachments.$[a].enrichments.extracted_text.started_at": now,
                "updated_at": now,
            }
        },
        array_filters=[{"a.attachment_id": attachment_id}],
    )
    if result.modified_count == 0:
        return None
    return await ManualMemory.find_one(ManualMemory.memory_id == memory_id)


def _body(memory: ManualMemory) -> str:
    sections = []
    if memory.note:
        sections.append(f"> {memory.note.strip()}")
    for attachment in memory.attachments:
        if attachment.description:
            sections.append(attachment.description.strip())
        if attachment.extracted_text:
            sections.append(f"## Extracted text\n\n{attachment.extracted_text.strip()}")
    return "\n\n".join(sections) or "Manual memory."


def write_memory_note(memory: ManualMemory, root: Path) -> str:
    tags = sorted({tag for attachment in memory.attachments for tag in attachment.tags})
    with vault_note_lock(memory.user_id):
        return write_manual_memory_note(
            memory.memory_id,
            root,
            frontmatter={
                "type": "manual_memory",
                "memory_id": memory.memory_id,
                "shared_at": memory.shared_at.isoformat(),
                "memory_at": memory.memory_at.isoformat() if memory.memory_at else None,
                "source_application": memory.source.get("application"),
                "tags": tags or None,
            },
            media_paths=[item.storage_path for item in memory.attachments],
            body=_body(memory),
        )


async def analyze_image(memory_id: str, attachment_id: str) -> dict[str, Any]:
    settings = manual_memory_settings()
    memory = await _claim(memory_id, attachment_id, settings.timeout_seconds)
    if memory is None:
        return {"status": "skipped"}
    attachment = _attachment(memory, attachment_id)
    assert attachment is not None
    resolver = MemoryScopeResolver()
    scope = MemoryScope(memory.user_id, memory.memory_space_id)
    if memory.memory_space_id:
        await resolver.require_space(scope, writable=True)
    root = resolver.vault_root(scope)
    path = root / attachment.storage_path
    try:
        data = await asyncio.to_thread(path.read_bytes)
        result = await run_structured_vision(
            f"{_PROMPT}\n\nContext:\n{json.dumps({'note': memory.note, 'source': memory.source})}",
            [(attachment.original_filename, data)],
            _SCHEMA,
            settings.vision,
        )
    except VisionUnavailable:
        for capability in ("description", "extracted_text"):
            attachment.enrichments[capability].state = "pending"
        await memory.save()
        raise
    except (VisionError, OSError) as exc:
        for capability in ("description", "extracted_text"):
            state = attachment.enrichments[capability]
            state.state = "failed"
            state.attempts += 1
            state.error = str(exc)[:500]
        await memory.save()
        return {"status": "failed", "reason": str(exc)[:200]}
    attachment.description = result["description"].strip()
    attachment.extracted_text = result["ocr_text"].strip()
    attachment.entities = result["entities"]
    attachment.tags = result["tags"]
    attachment.sensitive = result["sensitive"]
    attachment.metadata["app_or_site"] = result["app_or_site"].strip() or None
    for capability in ("description", "extracted_text"):
        state = attachment.enrichments[capability]
        state.state = "complete"
        state.completed_at = utcnow()
        state.error = None
    memory.updated_at = utcnow()
    memory.vault_path = await asyncio.to_thread(write_memory_note, memory, root)
    await memory.save()
    return {
        "status": "complete",
        "memory_id": memory_id,
        "attachment_id": attachment_id,
    }


async def process_manual_memory_images() -> dict[str, int | str]:
    rows = (
        await ManualMemory.find(
            {
                "attachments": {
                    "$elemMatch": {
                        "media_type": "image",
                        "enrichments.description.state": {"$in": ["pending", "failed"]},
                        "enrichments.description.attempts": {
                            "$lt": MAX_IMAGE_ANALYSIS_ATTEMPTS
                        },
                    }
                }
            }
        )
        .sort("-shared_at")
        .limit(_BATCH)
        .to_list()
    )
    counts = {"complete": 0, "failed": 0, "skipped": 0}
    for memory in rows:
        attachment = next(
            item
            for item in memory.attachments
            if item.media_type == "image"
            and item.enrichments["description"].state in {"pending", "failed"}
        )
        try:
            outcome = await analyze_image(memory.memory_id, attachment.attachment_id)
        except VisionUnavailable:
            return {**counts, "status": "unavailable"}
        counts[outcome["status"]] += 1
    return counts
