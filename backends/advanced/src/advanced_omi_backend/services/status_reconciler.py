"""Conversation processing-status reconciler.

``Conversation.processing_status`` is a denormalized field derived from facts
(does an active transcript exist?). The terminal finalizer job owns it during
normal processing, but jobs crash, get killed on timeout, or leave dependents
deferred forever — any of which can leave the field out of sync with reality.

This reconciler recomputes the status from facts via ``Conversation.apply_status``
and is both:

- a one-off migration / admin action (``reconcile_conversation_statuses``), and
- a periodic in-process self-heal (``run_status_reconciler``),

so drift can never persist. It is the same philosophy as the audio-chunk repair:
trust the source of truth, recompute the cached field.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# A conversation older than this with no terminal marker is treated as "settled"
# (no more jobs will run), so "no transcript" resolves to FAILED rather than ACTIVE.
STALE_AFTER_HOURS = float(os.getenv("STATUS_RECONCILE_STALE_HOURS", "6"))
RECONCILE_INTERVAL_SECS = int(os.getenv("STATUS_RECONCILE_INTERVAL_SECS", "1800"))


async def reconcile_conversation_statuses(
    *,
    stale_after_hours: float = STALE_AFTER_HOURS,
    dry_run: bool = False,
    user_id: Optional[str] = None,
) -> dict:
    """Recompute processing_status for non-deleted conversations from facts.

    A conversation is treated as ``settled`` (terminal) when it has a
    ``completed_at`` timestamp OR is older than ``stale_after_hours`` — in either
    case no further pipeline work is expected, so the absence of a transcript is a
    real failure rather than "not yet". Returns a summary with the changes applied.
    """
    from advanced_omi_backend.models.conversation import Conversation

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=stale_after_hours)

    query: dict = {"deleted": {"$ne": True}}
    if user_id:
        query["user_id"] = user_id

    scanned = 0
    changes: list[dict] = []

    async for conv in Conversation.find(query):
        scanned += 1

        created = conv.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        settled = conv.completed_at is not None or (
            created is not None and created < stale_cutoff
        )

        before = (conv.processing_status, conv.failure_stage)
        if conv.apply_status(settled=settled):
            changes.append(
                {
                    "conversation_id": conv.conversation_id,
                    "from": before[0],
                    "to": conv.processing_status,
                    "failure_stage": conv.failure_stage,
                }
            )
            if not dry_run:
                await conv.save()

    if changes:
        logger.info(
            f"🩺 Status reconcile: {'(dry-run) ' if dry_run else ''}"
            f"updated {len(changes)}/{scanned} conversation(s)"
        )

    return {
        "scanned": scanned,
        "changed": len(changes),
        "dry_run": dry_run,
        "details": changes[:100],
    }


async def run_status_reconciler() -> None:
    """Periodic in-process self-heal. Cancelled on app shutdown."""
    logger.info(
        f"🩺 Status reconciler started (every {RECONCILE_INTERVAL_SECS}s; "
        f"stale-after {STALE_AFTER_HOURS:.0f}h)"
    )
    while True:
        try:
            await asyncio.sleep(RECONCILE_INTERVAL_SECS)
            await reconcile_conversation_statuses()
        except asyncio.CancelledError:
            logger.info("🩺 Status reconciler stopped")
            raise
        except Exception as e:
            logger.error(f"Status reconciler pass failed: {e}", exc_info=True)
