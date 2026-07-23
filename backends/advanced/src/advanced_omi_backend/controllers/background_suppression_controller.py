"""Review surface for the background-suppression ledger.

Serves the conversation-detail chip: what was marked background (or would be,
for backfilled shadow entries), grouped by acoustic cluster, and applies the
two user verdicts — restore ("that's important speech") and confirm ("yes,
that's background"). Design: Docs/background-suppression-design.md.
"""

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi.responses import JSONResponse

from advanced_omi_backend.constants import UNKNOWN_SPEAKER_PREFIX
from advanced_omi_backend.controllers.background_bucket_controller import (
    add_background_clip,
)
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.users import User
from advanced_omi_backend.workers import background_suppression

logger = logging.getLogger(__name__)

EXEMPLARS_PER_CONFIRM = 5


def _ledger():
    return background_suppression._ledger_collection()


def _overrides():
    return background_suppression._overrides_collection()


async def get_conversation_suppressions(user: User, conversation_id: str) -> dict:
    """The ledger for one conversation, grouped by cluster for the chip/panel."""
    user_id = str(user.user_id)
    docs = [
        doc
        async for doc in _ledger()
        .find(
            {"user_id": user_id, "conversation_id": conversation_id},
            {"_id": 0, "user_id": 0},
        )
        .sort("segment_start", 1)
    ]
    clusters: dict[str, dict] = {}
    for doc in docs:
        signature = doc.get("cluster_signature") or "unclustered"
        cluster = clusters.setdefault(
            signature,
            {
                "cluster_signature": signature,
                "segments": [],
                "statuses": {},
                "zones": {},
                "max_background_similarity": 0.0,
            },
        )
        cluster["segments"].append(doc)
        cluster["statuses"][doc["status"]] = cluster["statuses"].get(doc["status"], 0) + 1
        cluster["zones"][doc["zone"]] = cluster["zones"].get(doc["zone"], 0) + 1
        cluster["max_background_similarity"] = max(
            cluster["max_background_similarity"],
            float(doc.get("background_similarity") or 0.0),
        )
    ordered = sorted(
        clusters.values(),
        key=lambda c: (-len(c["segments"]), -c["max_background_similarity"]),
    )
    counts: dict[str, int] = {}
    for doc in docs:
        counts[doc["status"]] = counts.get(doc["status"], 0) + 1
    override = await background_suppression.get_subject_override(
        user_id, conversation_id
    )
    return {
        "conversation_id": conversation_id,
        "total": len(docs),
        "status_counts": counts,
        "clusters": ordered,
        "subject_override": bool(override),
    }


async def decide_suppression_cluster(
    user: User,
    conversation_id: str,
    cluster_signature: str,
    decision: Literal["restore", "confirm"],
) -> dict:
    """Apply a user verdict to one cluster of marked segments.

    restore — "important speech": segments get their previous identification
    back, the ledger rows flip to ``restored`` (sticky), and a conversation-
    scoped role override records the intent. Never creates global foreground
    exemplars (a rescued media voice must stay a media voice elsewhere).

    confirm — "yes, background": rows flip to ``confirmed`` (sticky) and up to
    a few representative clips join the background bucket as exemplars.
    """
    user_id = str(user.user_id)
    query = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "cluster_signature": cluster_signature,
        "status": {"$nin": sorted(background_suppression.STICKY_STATUSES)},
    }
    docs = [doc async for doc in _ledger().find(query)]
    if not docs:
        return JSONResponse(
            status_code=404, content={"error": "No undecided segments in cluster"}
        )
    now = datetime.now(timezone.utc)

    if decision == "restore":
        applied = [doc for doc in docs if doc["status"] == "applied"]
        if applied:
            await _restore_segment_labels(conversation_id, applied)
        await _overrides().update_one(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "cluster_signature": cluster_signature,
            },
            {"$set": {"role": "subject", "set_by": user_id, "created_at": now}},
            upsert=True,
        )
        new_status = "restored"
    else:
        ranked = sorted(
            docs, key=lambda d: -float(d.get("background_similarity") or 0.0)
        )
        added = 0
        for doc in ranked[:EXEMPLARS_PER_CONFIRM]:
            created = await add_background_clip(
                conversation_id,
                float(doc["segment_start"]),
                float(doc["segment_end"]),
                bucket_type=doc.get("bucket_type") or "background_speech",
                source="suppression_review",
                user=user,
            )
            if created:
                added += 1
        logger.info(
            "Suppression confirm: %d exemplars added from %s/%s",
            added,
            conversation_id[:8],
            cluster_signature,
        )
        new_status = "confirmed"

    await _ledger().update_many(
        query,
        {"$set": {"status": new_status, "reviewed_at": now, "reviewed_by": user_id}},
    )
    return {
        "conversation_id": conversation_id,
        "cluster_signature": cluster_signature,
        "decision": decision,
        "segments": len(docs),
    }


async def _restore_segment_labels(conversation_id: str, docs: list[dict]) -> None:
    """Put back the pre-marking identification on relabelled segments."""
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation or not conversation.transcript_versions:
        return
    version = conversation.transcript_versions[-1]
    by_start = {
        background_suppression.segment_key(doc["segment_start"]): doc for doc in docs
    }
    changed = 0
    for segment in version.segments or []:
        key = background_suppression.segment_key(segment.start)
        doc = by_start.get(key)
        if doc is None:
            continue
        previous = doc.get("previous_identified_as")
        segment.speaker = previous or UNKNOWN_SPEAKER_PREFIX
        segment.identified_as = previous
        segment.confidence = doc.get("previous_confidence")
        changed += 1
    if changed:
        await conversation.save()
        logger.info(
            "Restored %d segment labels in conversation %s",
            changed,
            conversation_id[:8],
        )
