"""Give existing audio chunks the absolute capture time they were never written with.

``AudioChunkDocument.captured_at`` anchors a chunk in wall-clock time independently of
whichever conversation owns it. Chunks written before it existed have none, so nothing
recorded so far can be re-bounded, stitched back together, or asked *when did this
happen* without going through its parent — which is exactly the coupling the field
removes.

The anchor is recoverable, but not from one place, and not equally well everywhere. A
wrong anchor is worse than no anchor: a null says "unknown" and can be fixed later,
while a plausible-but-wrong timestamp silently corrupts every future stitch. So this
resolves an anchor per conversation from the best source available and **skips** what
it cannot justify, reporting the skips rather than guessing.

    uv run python scripts/backfill_chunk_capture_time.py            # dry run
    uv run python scripts/backfill_chunk_capture_time.py --apply
"""

import argparse
import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import AudioEvidenceSpan

logger = logging.getLogger("backfill")

# Audio that was recorded elsewhere and handed to Chronicle later: created_at is the
# moment it arrived, which says nothing about when the sound happened.
#
# ``data_purpose`` is the reliable discriminator, not the device name. The mined
# speaker clips carry the device ``speaker-mining`` and no external_source_type, and
# an earlier version of this script anchored all 343 of them to the minute they were
# mined — producing 4,224 chunks that all claimed the same 72 minutes of 2026-07-17.
SKIP_PURPOSES = {"annotation"}
SKIP_SOURCE_TYPES = {"annotation_dataset"}
SKIP_DEVICES = {"upload"}
MAX_DERIVED_DEPTH = 5


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _device(conversation: Conversation) -> str:
    parts = (conversation.client_id or "").split("-", 1)
    return parts[1] if len(parts) > 1 else ""


async def resolve_anchor(
    conversation: Conversation,
    spans: dict[str, datetime],
    by_id: dict[str, Conversation],
    depth: int = 0,
) -> tuple[datetime | None, str]:
    """Absolute time of this conversation's ``start_time == 0``, and how we know it."""
    if conversation.data_purpose in SKIP_PURPOSES:
        return None, "skipped:annotation_clip"
    if conversation.external_source_type in SKIP_SOURCE_TYPES:
        return None, "skipped:imported_audio"
    if _device(conversation) in SKIP_DEVICES:
        return None, "skipped:file_upload"

    # A split or trim child's created_at is when the operation ran, not when the audio
    # happened. Its parent knows, and its time_range says where inside the parent it
    # begins — the same formula covers both operations because a leading-silence
    # remnant records a range starting at zero.
    derived = conversation.derived_from
    if derived is not None:
        if depth >= MAX_DERIVED_DEPTH:
            return None, "skipped:derivation_too_deep"
        sources = derived.source_conversation_ids or []
        parent = by_id.get(sources[0]) if sources else None
        if parent is None:
            return None, "skipped:parent_gone"
        anchor, _ = await resolve_anchor(parent, spans, by_id, depth + 1)
        if anchor is None:
            return None, "skipped:parent_unanchored"
        offset = (derived.time_range or [0.0])[0]
        return anchor + timedelta(seconds=float(offset)), f"derived:{derived.operation}"

    # Continuous capture records the true span of the audio it assembled.
    span = spans.get(conversation.conversation_id)
    if span is not None:
        return _as_utc(span), "evidence_span"

    # Everything else is live capture, where the conversation was created as the audio
    # arrived, so created_at is the audio's start to within seconds.
    return _as_utc(conversation.created_at), "conversation_created_at"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--batch", type=int, default=2000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://mongo:27017"))
    await init_beanie(
        database=client.chronicle,
        document_models=[Conversation, AudioChunkDocument, AudioEvidenceSpan],
    )

    conversations = await Conversation.find_all().to_list()
    by_id = {c.conversation_id: c for c in conversations}
    spans = {
        span.conversation_id: span.started_at
        for span in await AudioEvidenceSpan.find(
            AudioEvidenceSpan.conversation_id != None  # noqa: E711
        ).to_list()
        if span.conversation_id
    }
    logger.info("%d conversations, %d evidence spans", len(conversations), len(spans))

    collection = AudioChunkDocument.get_pymongo_collection()
    reasons: Counter = Counter()
    chunks_by_reason: Counter = Counter()
    cleared: Counter = Counter()
    operations: list[UpdateOne] = []
    written = exact = 0

    for conversation in conversations:
        anchor, reason = await resolve_anchor(conversation, spans, by_id)
        reasons[reason] += 1
        if anchor is None:
            # Also CLEAR anything a previous, less careful run anchored here. Rerunning
            # has to be able to withdraw a wrong answer, not just decline to add one.
            stale = await collection.count_documents(
                {
                    "conversation_id": conversation.conversation_id,
                    "captured_at": {"$ne": None},
                }
            )
            if stale:
                cleared[reason] += stale
                if args.apply:
                    await collection.update_many(
                        {"conversation_id": conversation.conversation_id},
                        {"$set": {"captured_at": None}},
                    )
        chunks = await collection.find(
            {"conversation_id": conversation.conversation_id, "captured_at": None},
            {"start_time": 1, "source_first_message_id": 1},
        ).to_list(length=None)
        if not chunks:
            continue
        chunks_by_reason[reason] += len(chunks)
        if anchor is None:
            continue
        for chunk in chunks:
            # The Redis stream ID is the moment the audio was appended, which beats
            # any anchor derived from the conversation around it.
            stamp = _from_stream_id(chunk.get("source_first_message_id"))
            if stamp is not None:
                exact += 1
            else:
                stamp = anchor + timedelta(seconds=float(chunk.get("start_time") or 0))
            operations.append(
                UpdateOne({"_id": chunk["_id"]}, {"$set": {"captured_at": stamp}})
            )
        if args.apply and len(operations) >= args.batch:
            await collection.bulk_write(operations)
            written += len(operations)
            operations = []

    if args.apply and operations:
        await collection.bulk_write(operations)
        written += len(operations)

    total = sum(chunks_by_reason.values())
    anchored = sum(
        n for r, n in chunks_by_reason.items() if not r.startswith("skipped")
    )
    logger.info("\nchunks needing an anchor: %d", total)
    for reason, count in sorted(chunks_by_reason.items(), key=lambda kv: -kv[1]):
        logger.info(
            "  %-28s %6d chunks  (%d conversations)", reason, count, reasons[reason]
        )
    logger.info(
        "\nanchored: %d of %d (%.0f%%), of which %d from an exact Redis timestamp",
        anchored,
        total,
        100 * anchored / max(1, total),
        exact,
    )
    logger.info("left null on purpose: %d", total - anchored)
    if cleared:
        logger.info("\nwithdrawn (anchored by an earlier run, should not have been):")
        for reason, count in cleared.most_common():
            logger.info("  %-28s %6d chunks", reason, count)
    logger.info("%s", f"WROTE {written}" if args.apply else "dry run — nothing written")


def _from_stream_id(value: str | None) -> datetime | None:
    if not value:
        return None
    milliseconds = str(value).split("-", 1)[0]
    if not milliseconds.isdigit():
        return None
    return datetime.fromtimestamp(int(milliseconds) / 1000.0, tz=timezone.utc)


if __name__ == "__main__":
    asyncio.run(main())
