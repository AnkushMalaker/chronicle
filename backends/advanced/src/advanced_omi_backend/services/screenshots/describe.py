"""Describe a shared screenshot so it becomes findable text.

The vault is searched with ripgrep over Markdown, so an image is only ever findable
if something writes words about it. This pass is that something: it looks at the
screenshot once, on arrival, and writes both item metadata and a ``Media/<digest>.md``
note. The note is the load-bearing half — it makes screenshots answerable by the
existing memory search with no new retrieval machinery.

Everything shared is described. The act of sharing is the privacy gate: Chronicle is
not ingesting a camera roll, the user picked this image deliberately. The ``sensitive``
flag the model returns is recorded so it can be acted on later, but it changes nothing
about what is stored today.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from bson import ObjectId

from advanced_omi_backend.models.device_input import DeviceInputItem, utcnow
from advanced_omi_backend.services.memory.vault_lock import vault_note_lock
from advanced_omi_backend.services.memory.vault_manager import ConvDocVaultManager
from advanced_omi_backend.services.memory.vault_media import write_media_note
from advanced_omi_backend.services.vision import (
    CodexVisionError,
    CodexVisionUnavailable,
    run_codex_vision,
)

from .config import MAX_DESCRIBE_ATTEMPTS, ScreenshotSettings, screenshot_settings

logger = logging.getLogger(__name__)

# Items handled per cron tick. Each is one Codex round trip.
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

_PROMPT = """\
Describe this screenshot the way the person who saved it would recall it later.

They kept it deliberately, so say what it actually shows, which app or site it came
from, and what it appears to be *for* — a ticket to use, a product to look up, a
comment worth remembering, an error to fix. Write `description` as a few plain
sentences someone could search months from now; do not narrate the interface chrome.

Transcribe the readable text into `ocr_text` verbatim, including names, numbers and
handles — that is what makes a half-remembered detail findable later. Put concrete
proper nouns (people, products, places, games, companies) in `entities`, and a few
short lowercase topic words in `tags`.

Set `sensitive` to true if the image shows credentials, one-time codes, financial
account detail, medical information, or private correspondence.
"""


def _iso_utc(value: datetime) -> str:
    """Mongo returns naive UTC, so restore the marker before writing it down."""

    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _context(item: DeviceInputItem) -> dict[str, Any]:
    return {
        "captured_at": _iso_utc(item.captured_at),
        "caption": item.metadata.get("caption"),
        "shared_from": item.metadata.get("origin_app"),
    }


def _note_body(result: dict[str, Any], item: DeviceInputItem) -> str:
    caption = (item.metadata.get("caption") or "").strip()
    sections = []
    if caption:
        sections.append(f"> {caption}")
    sections.append((result.get("description") or "").strip())
    ocr = (result.get("ocr_text") or "").strip()
    if ocr:
        sections.append(f"## Text\n\n{ocr}")
    return "\n\n".join(part for part in sections if part)


async def _claim(item_id: str, timeout_seconds: int) -> Optional[DeviceInputItem]:
    """Take exclusive ownership of one item, or return None if someone else has it.

    Both the on-arrival RQ job and the cron backstop can reach the same item. The
    conditional update is what stops them both paying for a Codex run on it.
    """
    stale_before = utcnow() - timedelta(seconds=2 * timeout_seconds)
    result = await DeviceInputItem.get_pymongo_collection().update_one(
        {
            "_id": ObjectId(item_id),
            "kind": "screenshot",
            "$or": [
                {"metadata.description_state": {"$in": [None, "pending", "failed"]}},
                # A describer that died mid-run leaves the item claimed forever
                # otherwise, exactly like /jobs/next reclaiming a stale device claim.
                {"metadata.description_started_at": {"$lt": stale_before}},
            ],
        },
        {
            "$set": {
                "metadata.description_state": "describing",
                "metadata.description_started_at": utcnow(),
            }
        },
    )
    if result.modified_count == 0:
        return None
    return await DeviceInputItem.get(item_id)


async def _record_failure(item: DeviceInputItem, error: str) -> None:
    attempts = int(item.metadata.get("description_attempts") or 0) + 1
    item.metadata = {
        **item.metadata,
        "description_state": "failed",
        "description_attempts": attempts,
        "description_error": error[:500],
    }
    await item.save()
    if attempts >= MAX_DESCRIBE_ATTEMPTS:
        logger.warning(
            "Screenshot %s left undescribed after %s attempts: %s",
            item.id,
            attempts,
            error[:200],
        )


async def describe_screenshot(item_id: str) -> dict[str, Any]:
    """Describe one screenshot and write its vault note.

    Returns a status dict rather than raising, so the RQ job and the cron backstop
    behave identically and neither strands the item in ``describing``.
    """
    settings = screenshot_settings()
    item = await _claim(item_id, settings.timeout_seconds)
    if item is None:
        return {"status": "skipped", "reason": "not claimable"}
    if not item.media_data:
        await _record_failure(item, "screenshot has no stored image bytes")
        return {"status": "failed", "reason": "no image bytes"}

    suffix = (item.media_content_type or "image/jpeg").split("/")[-1]
    prompt = f"{_PROMPT}\n\nContext:\n{json.dumps(_context(item), ensure_ascii=False)}"
    try:
        result = await run_codex_vision(
            prompt,
            [(f"screenshot.{suffix}", item.media_data)],
            _SCHEMA,
            settings.codex,
        )
    except CodexVisionUnavailable:
        # Codex being absent says nothing about this image, so release the claim
        # without spending one of its attempts.
        item.metadata = {**item.metadata, "description_state": "pending"}
        await item.save()
        raise
    except CodexVisionError as exc:
        await _record_failure(item, str(exc))
        return {"status": "failed", "reason": str(exc)[:200]}

    root = ConvDocVaultManager().user_root(item.user_id)
    note_path = await asyncio.to_thread(
        _write_note, item, result, root, _note_body(result, item)
    )

    item.metadata = {
        **item.metadata,
        "description_state": "described",
        "description": (result.get("description") or "").strip(),
        "ocr_text": (result.get("ocr_text") or "").strip(),
        "app_or_site": (result.get("app_or_site") or "").strip() or None,
        "entities": result.get("entities") or [],
        "tags": result.get("tags") or [],
        "sensitive": bool(result.get("sensitive")),
        "described_at": utcnow().isoformat(),
        "description_error": None,
    }
    item.vault_paths = [note_path]
    item.state = "promoted"
    await item.save()
    return {"status": "described", "item_id": str(item.id), "note": note_path}


def _write_note(
    item: DeviceInputItem, result: dict[str, Any], root: Path, body: str
) -> str:
    """Write the Media/ note under the vault lock, off the event loop."""

    with vault_note_lock(item.user_id):
        return write_media_note(
            item.promoted_path or "",
            item.content_hash or "",
            root,
            frontmatter={
                "source": "mobile",
                "item_id": str(item.id),
                "captured_at": _iso_utc(item.captured_at),
                "app": (result.get("app_or_site") or "").strip() or None,
                "tags": result.get("tags") or None,
                "sensitive": bool(result.get("sensitive")),
            },
            body=body,
            overwrite=True,
        )


async def process_screenshot_descriptions() -> dict[str, Any]:
    """Cron backstop for screenshots the on-arrival job never described."""

    try:
        screenshot_settings()
    except ValueError as exc:
        logger.warning("Screenshot description disabled: %s", exc)
        return {"described": 0, "failed": 0, "skipped": 0, "status": "misconfigured"}

    rows = (
        await DeviceInputItem.find(
            DeviceInputItem.kind == "screenshot",
            {
                "metadata.description_state": {"$in": [None, "pending", "failed"]},
                "metadata.description_attempts": {"$lt": MAX_DESCRIBE_ATTEMPTS},
            },
        )
        .sort("-captured_at")
        .limit(_BATCH)
        .to_list()
    )
    counts: dict[str, Any] = {"described": 0, "failed": 0, "skipped": 0}
    for row in rows:
        try:
            outcome = await describe_screenshot(str(row.id))
        except CodexVisionUnavailable as exc:
            # Service-level fault: stop the tick rather than walking the batch and
            # failing every item for a reason that has nothing to do with them.
            logger.warning(
                "Codex unavailable, deferring screenshot descriptions: %s", exc
            )
            counts["status"] = "unavailable"
            return counts
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
    return counts
