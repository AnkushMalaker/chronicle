"""Resolve a cited recording to whatever is live now.

An episode may cite recordings by ``conversation_id``, but a Conversation is a
semantic claim, not the audio. Split, merge, and trim replace or clip those claims
while immutable capture chunks stay exactly where they were.

- **dedup** — a ScreenPipe retry can leave two semantic claims over one capture;
- **merge/split** — re-bounding creates new range claims and soft-deletes sources;
- **silence trim** — the visible claim is clipped without deleting raw evidence.

Any of these leaves an episode pointing at a soft-deleted conversation, and promotion
only unhides live ones — so a real meeting the agent identified stays hidden. Measured
on this deployment, 126 of 638 episodes cited nothing live, including every one of the
15 generations of a "Webex call with Vatsal" that six generations had marked
conversational.

Rather than rewriting the agent's stored evidence, this resolves at read time, so it
also covers a dedup or trim that happens *after* the episode was published.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import TimelineAudioRange, TimelineEvidenceRef
from advanced_omi_backend.services.audio_claims import load_chunks_by_id

logger = logging.getLogger(__name__)

# A dead recording resolves to the live audio covering its span. Depth bounds the
# derived_into walk; the fan-out cap keeps a pathological span from returning half the
# corpus when something has gone wrong upstream.
MAX_LINEAGE_DEPTH = 8
MAX_FANOUT = 12


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def build_audio_ranges(
    *,
    started_at: datetime,
    ended_at: datetime,
    evidence_refs: Iterable[TimelineEvidenceRef],
    related_conversation_ids: Iterable[str] = (),
) -> list[TimelineAudioRange]:
    """Freeze an episode's audio claim onto stable chunk ids and absolute time.

    Candidate containers come from assembly-recorded evidence plus agent-supplied
    lineage. The resulting references no longer depend on those containers: later
    split/merge/trim operations move the same chunk documents without changing ids or
    ``captured_at``.
    """
    candidates = {str(item) for item in related_conversation_ids if item}
    for ref in evidence_refs:
        conversation_id = ref.metadata.get("conversation_id")
        if conversation_id:
            candidates.add(str(conversation_id))
    if not candidates:
        return []

    owners = await resolve_live_recordings(candidates)
    if not owners:
        return []
    start, end = _utc(started_at), _utc(ended_at)
    conversations = await Conversation.find(
        {"conversation_id": {"$in": sorted(owners)}, "deleted": {"$ne": True}}
    ).to_list()
    ranges: list[TimelineAudioRange] = []
    seen: set[tuple] = set()
    for conversation in conversations:
        for claim in conversation.audio_ranges:
            claim_start = max(start, _utc(claim.started_at))
            claim_end = min(end, _utc(claim.ended_at))
            if claim_end <= claim_start:
                continue
            chunks = await load_chunks_by_id(claim.chunk_ids)
            chunk_ids = [
                str(chunk.id)
                for chunk in chunks
                if _utc(chunk.captured_at) < claim_end
                and _utc(chunk.captured_at) + timedelta(seconds=chunk.duration)
                > claim_start
            ]
            if not chunk_ids:
                continue
            identity = (
                claim.capture_source_id,
                tuple(chunk_ids),
                claim_start,
                claim_end,
            )
            if identity in seen:
                continue
            seen.add(identity)
            ranges.append(
                TimelineAudioRange(
                    capture_source_id=claim.capture_source_id,
                    time_basis=claim.time_basis,
                    capture_session_ids=claim.capture_session_ids,
                    chunk_ids=chunk_ids,
                    started_at=claim_start,
                    ended_at=claim_end,
                    source_stream=next(
                        (
                            chunk.source_stream
                            for chunk in chunks
                            if chunk.source_stream
                        ),
                        None,
                    ),
                    conversation_ids=[conversation.conversation_id],
                )
            )
    ranges.sort(key=lambda item: (item.started_at, item.source_stream or ""))
    return ranges


async def resolve_live_recordings(conversation_ids: Iterable[str]) -> set[str]:
    """Live recordings covering the audio the given ids referred to."""
    requested = list(dict.fromkeys(str(item) for item in conversation_ids if item))
    conversations = await _conversation_documents(requested)
    resolved: set[str] = set()
    deleted: dict[str, dict[str, Any]] = {}
    for conversation_id in requested:
        conversation = conversations.get(conversation_id)
        if conversation is None:
            continue
        if not conversation.get("deleted", False):
            resolved.add(conversation_id)
            continue
        deleted[conversation_id] = conversation

    descendants = await _live_descendants(deleted)
    for conversation_id, conversation in deleted.items():
        lineage = descendants.get(conversation_id, set())
        if lineage:
            resolved |= lineage
            continue
        # Dedup records no lineage — the surviving twin was ingested separately and
        # never knew about this copy. What identifies them as the same recording is
        # that they cover the same wall-clock audio on the same capture stream.
        resolved |= await _live_over_same_audio(
            conversation_id=conversation_id,
            client_id=conversation.get("client_id"),
        )
    return resolved


async def _conversation_documents(
    conversation_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    ids = sorted(set(conversation_ids))
    if not ids:
        return {}
    documents = (
        await Conversation.get_pymongo_collection()
        .find(
            {"conversation_id": {"$in": ids}},
            {
                "_id": 0,
                "conversation_id": 1,
                "deleted": 1,
                "derived_into": 1,
                "client_id": 1,
                "audio_ranges": 1,
            },
        )
        .to_list(length=None)
    )
    return {document["conversation_id"]: document for document in documents}


async def _live_descendants(
    roots: dict[str, dict[str, Any]],
) -> dict[str, set[str]]:
    """Resolve all lineage trees with one raw Mongo query per tree depth."""
    found = {root_id: set() for root_id in roots}
    frontier = {
        root_id: set(root.get("derived_into") or []) for root_id, root in roots.items()
    }
    visited = {root_id: set() for root_id in roots}

    for _depth in range(MAX_LINEAGE_DEPTH):
        child_ids = set().union(*frontier.values()) if frontier else set()
        if not child_ids:
            break
        children = await _conversation_documents(child_ids)
        next_frontier: dict[str, set[str]] = {}
        for root_id, requested_ids in frontier.items():
            pending: set[str] = set()
            for child_id in requested_ids - visited[root_id]:
                visited[root_id].add(child_id)
                child = children.get(child_id)
                if child is None:
                    continue
                if not child.get("deleted", False):
                    found[root_id].add(child_id)
                else:
                    pending.update(child.get("derived_into") or [])
            next_frontier[root_id] = pending
        frontier = next_frontier
    return found


async def _live_over_same_audio(
    *, conversation_id: str, client_id: str | None
) -> set[str]:
    """Live recordings on the same stream whose audio overlaps this one's span."""
    source = await Conversation.get_pymongo_collection().find_one(
        {"conversation_id": conversation_id}, {"audio_ranges.chunk_ids": 1}
    )
    chunk_ids = [
        chunk_id
        for audio_range in (source or {}).get("audio_ranges", [])
        for chunk_id in audio_range.get("chunk_ids", [])
    ]
    if not chunk_ids:
        return set()
    # Same capture stream, or it is different audio that merely happened at the same
    # moment — the other node, or the other direction of the same call. Fetch raw
    # projected rows in one query; materialising every full Conversation model here
    # was the dominant publish-time cost for large generated days.
    documents = (
        await Conversation.get_pymongo_collection()
        .find(
            {
                "conversation_id": {"$ne": conversation_id},
                "deleted": {"$ne": True},
                "client_id": client_id,
                "audio_ranges.chunk_ids": {"$in": chunk_ids},
            },
            {"_id": 0, "conversation_id": 1},
        )
        .to_list(length=MAX_FANOUT + 1)
    )
    live = {document["conversation_id"] for document in documents[:MAX_FANOUT]}
    if len(documents) > MAX_FANOUT:
        logger.warning(
            "recording %s resolves to more than %d live recordings; truncating",
            conversation_id[:8],
            MAX_FANOUT,
        )
    return live
