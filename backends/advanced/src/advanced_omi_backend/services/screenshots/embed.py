"""Index shared screenshots in the ColPali service.

**The backend pushes; the service does not pull.** ``DeviceInputJob`` is keyed by
``source_id`` and its puller authenticates a ``CaptureSource`` device token, but a GPU
node produces no device input — pull would mean inventing a compute-node provider, a
pairing flow, a long-lived token sitting on a GPU box, *and* a new device-token
endpoint that serves image bytes (no device endpoint exposes those today). That is
real new attack surface for no benefit. Push is what ASR, TTS and speaker recognition
already do.

The node hosting the service is a desktop that sleeps and reboots. So the health
probe comes first, and an unreachable service ends the tick **without touching any
item's state**: a sleeping GPU must never consume a screenshot's retry budget. The
work is simply still pending on the next tick.
"""

import logging
from datetime import timedelta
from typing import Any

from advanced_omi_backend.models.device_input import DeviceInputItem, utcnow
from advanced_omi_backend.services.colpali_client import (
    embed_image,
    health,
    indexed_documents,
)

logger = logging.getLogger(__name__)

# Items per tick. Each is one image upload plus a GPU forward pass.
_BATCH = 20
# Attempts before an item is left alone. Generous because the common failure here is
# an unhealthy node rather than a bad image — and that case increments nothing.
_MAX_ATTEMPTS = 8
# Backoff between attempts, capped so a long-broken node still retries twice a day.
_MAX_BACKOFF_MINUTES = 360


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2**attempts, _MAX_BACKOFF_MINUTES))


async def _reconcile(user_id: str, model: str) -> int:
    """Reset items the service no longer holds, or that a different model embedded.

    This is what makes a wiped ``/index`` volume or a ``COLPALI_MODEL`` change
    self-healing instead of a silent permanent gap.
    """
    doc_ids = await indexed_documents(user_id)
    if doc_ids is None:
        return 0
    known = set(doc_ids)
    rows = await DeviceInputItem.find(
        DeviceInputItem.user_id == user_id,
        DeviceInputItem.kind == "screenshot",
        {"metadata.embed_state": "embedded"},
    ).to_list()
    reset = 0
    for row in rows:
        stale_model = row.metadata.get("embed_model") != model
        if str(row.id) in known and not stale_model:
            continue
        row.metadata = {
            **row.metadata,
            "embed_state": "pending",
            "embed_attempts": 0,
            "embed_next_attempt_at": None,
        }
        await row.save()
        reset += 1
    if reset:
        logger.info("Re-queued %s screenshots for %s after reconcile", reset, user_id)
    return reset


async def process_screenshot_embeddings() -> dict[str, Any]:
    """Index screenshots that the visual search service does not have yet."""

    status = await health()
    if status is None:
        # Not configured, or the node is asleep. Either way this says nothing about
        # any individual screenshot, so no counters move.
        return {"embedded": 0, "failed": 0, "status": "unavailable"}
    model = status.get("model") or "unknown"

    now = utcnow()
    rows = (
        await DeviceInputItem.find(
            DeviceInputItem.kind == "screenshot",
            {
                "metadata.embed_state": {"$in": [None, "pending", "failed"]},
                "metadata.embed_attempts": {"$lt": _MAX_ATTEMPTS},
                "$or": [
                    {"metadata.embed_next_attempt_at": None},
                    {"metadata.embed_next_attempt_at": {"$lte": now}},
                ],
            },
        )
        .sort("-captured_at")
        .limit(_BATCH)
        .to_list()
    )

    embedded = failed = 0
    users: set[str] = set()
    for row in rows:
        users.add(row.user_id)
        if not row.media_data:
            row.metadata = {**row.metadata, "embed_state": "unavailable"}
            await row.save()
            continue
        try:
            await embed_image(
                str(row.id),
                row.user_id,
                row.media_data,
                row.media_content_type or "image/jpeg",
                {
                    "item_id": str(row.id),
                    "captured_at": row.captured_at.isoformat(),
                    "caption": row.metadata.get("caption"),
                    # Carried so a search hit is readable without a second Mongo
                    # round trip to render the result.
                    "description": row.metadata.get("description"),
                    "app_or_site": row.metadata.get("app_or_site"),
                    "tags": row.metadata.get("tags") or [],
                    "content_hash": row.content_hash,
                },
            )
        except Exception as exc:
            attempts = int(row.metadata.get("embed_attempts") or 0) + 1
            row.metadata = {
                **row.metadata,
                "embed_state": "failed",
                "embed_attempts": attempts,
                "embed_error": str(exc)[:300],
                "embed_next_attempt_at": now + _backoff(attempts),
            }
            await row.save()
            failed += 1
            continue
        row.metadata = {
            **row.metadata,
            "embed_state": "embedded",
            "embed_model": model,
            "embed_error": None,
            "embed_next_attempt_at": None,
            "embedded_at": now.isoformat(),
        }
        await row.save()
        embedded += 1

    # Reconcile every user with screenshots, not just those in this batch: a wiped
    # index leaves nothing pending, so batch-scoped reconciliation would never
    # notice it. The distinct set is tiny (one user on a personal deployment).
    reconciled = 0
    all_users = await DeviceInputItem.get_pymongo_collection().distinct(
        "user_id", {"kind": "screenshot"}
    )
    for user_id in set(all_users) | users:
        reconciled += await _reconcile(user_id, model)

    return {
        "embedded": embedded,
        "failed": failed,
        "reconciled": reconciled,
        "model": model,
    }
