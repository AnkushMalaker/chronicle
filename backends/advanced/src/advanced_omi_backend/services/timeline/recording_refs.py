"""Resolve a cited recording to whatever is live now.

An episode cites recordings by ``conversation_id``, but a conversation id is a
container, not the audio. Three ordinary operations replace the container while the
audio stays exactly where it was:

- **dedup** — a ScreenPipe ingest retry uploads the same span twice, and the sweep
  soft-deletes one copy (``duplicate_screenpipe_ingest_retry``);
- **merge/split** — re-bounding moves chunks to a new conversation and soft-deletes
  the old one, recording ``derived_into``;
- **silence trim** — the remnant is soft-deleted and keeps the silence.

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
from typing import Iterable

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import TimelineAudioRange, TimelineEvidenceRef

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
    documents = (
        await AudioChunkDocument.find(
            {
                "conversation_id": {"$in": sorted(owners)},
                "captured_at": {"$lt": end},
                "deleted": {"$ne": True},
            }
        )
        .sort("+captured_at")
        .to_list()
    )
    chunks = [
        chunk
        for chunk in documents
        if chunk.captured_at is not None
        and _utc(chunk.captured_at) + timedelta(seconds=chunk.duration) > start
    ]
    if not chunks:
        return []

    # Keep concurrent input/output streams distinct. Within a stream, start a new
    # range at a real capture gap; adjacent chunks form one playable program section.
    grouped: dict[str, list[AudioChunkDocument]] = {}
    for chunk in chunks:
        key = chunk.source_stream or f"conversation:{chunk.conversation_id}"
        grouped.setdefault(key, []).append(chunk)

    ranges: list[TimelineAudioRange] = []
    for source, source_chunks in grouped.items():
        source_chunks.sort(key=lambda row: _utc(row.captured_at))  # type: ignore[arg-type]
        runs: list[list[AudioChunkDocument]] = []
        for chunk in source_chunks:
            if not runs:
                runs.append([chunk])
                continue
            previous = runs[-1][-1]
            previous_end = _utc(previous.captured_at) + timedelta(  # type: ignore[arg-type]
                seconds=previous.duration
            )
            if _utc(chunk.captured_at) - previous_end > timedelta(seconds=0.25):  # type: ignore[arg-type]
                runs.append([chunk])
            else:
                runs[-1].append(chunk)
        for run in runs:
            run_start = max(start, _utc(run[0].captured_at))  # type: ignore[arg-type]
            run_end = min(
                end,
                _utc(run[-1].captured_at) + timedelta(seconds=run[-1].duration),  # type: ignore[arg-type]
            )
            if run_end <= run_start:
                continue
            ranges.append(
                TimelineAudioRange(
                    chunk_ids=[str(chunk.id) for chunk in run],
                    started_at=run_start,
                    ended_at=run_end,
                    source_stream=(
                        None if source.startswith("conversation:") else source
                    ),
                    conversation_ids=sorted({chunk.conversation_id for chunk in run}),
                )
            )
    ranges.sort(key=lambda item: (item.started_at, item.source_stream or ""))
    return ranges


async def resolve_live_recordings(conversation_ids: Iterable[str]) -> set[str]:
    """Live recordings covering the audio the given ids referred to."""
    resolved: set[str] = set()
    for conversation_id in dict.fromkeys(conversation_ids):
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if conversation is None:
            continue
        if not conversation.deleted:
            resolved.add(conversation_id)
            continue
        lineage = await _live_descendants(conversation)
        if lineage:
            resolved |= lineage
            continue
        # Dedup records no lineage — the surviving twin was ingested separately and
        # never knew about this copy. What identifies them as the same recording is
        # that they cover the same wall-clock audio on the same capture stream.
        resolved |= await _live_over_same_audio(conversation)
    return resolved


async def _live_descendants(conversation: Conversation, depth: int = 0) -> set[str]:
    if depth >= MAX_LINEAGE_DEPTH:
        return set()
    found: set[str] = set()
    for child_id in conversation.derived_into or []:
        child = await Conversation.find_one(Conversation.conversation_id == child_id)
        if child is None:
            continue
        if not child.deleted:
            found.add(child_id)
        else:
            found |= await _live_descendants(child, depth + 1)
    return found


async def _live_over_same_audio(conversation: Conversation) -> set[str]:
    """Live recordings on the same stream whose audio overlaps this one's span."""
    collection = AudioChunkDocument.get_pymongo_collection()
    bounds = await collection.aggregate(
        [
            {
                "$match": {
                    "conversation_id": conversation.conversation_id,
                    "deleted": {"$ne": True},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "first": {"$min": "$captured_at"},
                    "last": {"$max": "$captured_at"},
                }
            },
        ]
    ).to_list(length=1)
    if not bounds or bounds[0]["first"] is None:
        return set()

    owners = await collection.distinct(
        "conversation_id",
        {
            "captured_at": {"$gte": bounds[0]["first"], "$lte": bounds[0]["last"]},
            "deleted": {"$ne": True},
        },
    )
    live: set[str] = set()
    for owner_id in owners:
        if owner_id == conversation.conversation_id:
            continue
        owner = await Conversation.find_one(Conversation.conversation_id == owner_id)
        # Same capture stream, or it is different audio that merely happened at the
        # same moment — the other node, or the other direction of the same call.
        if owner is None or owner.deleted or owner.client_id != conversation.client_id:
            continue
        live.add(owner_id)
        if len(live) >= MAX_FANOUT:
            logger.warning(
                "recording %s resolves to more than %d live recordings; truncating",
                conversation.conversation_id[:8],
                MAX_FANOUT,
            )
            break
    return live
