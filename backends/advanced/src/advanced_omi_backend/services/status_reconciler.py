"""Conversation processing-status reconciler.

``Conversation.processing_status`` is a denormalized field derived from facts
(does an active transcript exist?). The terminal finalizer job owns it during
normal processing.

Steady-state recovery is event-driven: the post-conversation chain uses RQ
``Retry`` + ``Dependency(allow_failure=True)`` + an ``on_failure`` callback, so a
crashed/abandoned job is retried, its dependents are still promoted (the finalizer
always runs and reconciles status), and the failure surfaces as a system event.
No periodic poll is needed.

This reconciler recomputes the status from facts via ``Conversation.derive_status``
and remains as a *backstop*:

- a one-shot sweep on app startup (heals drift left before this version, or by a
  failure callback that itself died), and
- an on-demand admin action (``reconcile_conversation_statuses`` via the admin
  endpoint).

It is the same philosophy as the audio-chunk repair: trust the source of truth,
recompute the cached field.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from pymongo import UpdateOne

from advanced_omi_backend.models.conversation import Conversation

logger = logging.getLogger(__name__)

# A conversation older than this with no terminal marker is treated as "settled"
# (no more jobs will run), so "no transcript" resolves to FAILED rather than ACTIVE.
STALE_AFTER_HOURS = float(os.getenv("STATUS_RECONCILE_STALE_HOURS", "6"))


def _status_projection_pipeline(query: dict) -> list[dict]:
    """Scan every conversation without transferring or hydrating any of them.

    Only five small fields and one boolean decide the status, but a conversation's
    transcripts are its largest field by orders of magnitude: this corpus is 871
    documents totalling 587 MB, averaging 690 KB each. Loading them as Beanie models
    to read ``processing_status`` moved all of that through the event loop — the scan
    ran as a background task, which spaces the cost out but does not make it
    asynchronous, so the loop froze in 185-503 ms slices for over a minute at every
    boot, and the allocation churn drove generation-2 collections on top.

    ``has_transcript`` is the only derived value, and whether the active version's text
    is non-empty is decidable in the query. The rules themselves stay in
    :meth:`Conversation.derive_status`.
    """
    active_version = {
        "$first": {
            "$filter": {
                "input": {"$ifNull": ["$transcript_versions", []]},
                "cond": {"$eq": ["$$this.version_id", "$active_transcript_version"]},
            }
        }
    }
    return [
        {"$match": query},
        {
            "$project": {
                "conversation_id": 1,
                "created_at": 1,
                "completed_at": 1,
                "processing_status": 1,
                "failure_stage": 1,
                "has_transcript": {
                    "$let": {
                        "vars": {"active": active_version},
                        "in": {
                            "$ne": [
                                {
                                    "$trim": {
                                        "input": {
                                            "$ifNull": ["$$active.transcript", ""]
                                        }
                                    }
                                },
                                "",
                            ]
                        },
                    }
                },
            }
        },
    ]


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
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=stale_after_hours)

    query: dict = {"deleted": {"$ne": True}}
    if user_id:
        query["user_id"] = user_id

    scanned = 0
    changes: list[dict] = []
    updates: list[UpdateOne] = []

    collection = Conversation.get_pymongo_collection()
    async for row in collection.aggregate(_status_projection_pipeline(query)):
        scanned += 1

        created = row.get("created_at")
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        settled = row.get("completed_at") is not None or (
            created is not None and created < stale_cutoff
        )

        before = (row.get("processing_status"), row.get("failure_stage"))
        after = Conversation.derive_status(
            has_transcript=bool(row.get("has_transcript")), settled=settled
        )
        if after == before:
            continue

        changes.append(
            {
                "conversation_id": row.get("conversation_id"),
                "from": before[0],
                "to": after[0],
                "failure_stage": after[1],
            }
        )
        updates.append(
            UpdateOne(
                {"_id": row["_id"]},
                {"$set": {"processing_status": after[0], "failure_stage": after[1]}},
            )
        )

    if updates and not dry_run:
        await collection.bulk_write(updates, ordered=False)

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
