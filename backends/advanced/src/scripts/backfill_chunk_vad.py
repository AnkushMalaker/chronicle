#!/usr/bin/env python3
"""Score every audio chunk that has no VAD scores.

Re-bounding a recording reads the stored per-chunk speech profile to decide
where to cut. Audio nobody has scored produces no speech intervals, which is
indistinguishable from silence unless the caller checks -- and read as silence
it puts the cut back at exactly the blind target the re-bound exists to remove.
``reset_recording_bounds`` therefore holds those windows back, and this fills
them in so they can be re-bound too.

    uv run python src/scripts/backfill_chunk_vad.py            # report only
    uv run python src/scripts/backfill_chunk_vad.py --apply

Scoring is per conversation because that is the unit VAD state is kept for, and
a conversation is skipped once every one of its chunks carries scores, so an
interrupted run resumes where it stopped.
"""

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User
from advanced_omi_backend.utils.vad_analysis import analyze_conversation_audio

logger = logging.getLogger("backfill-vad")


async def unscored_conversations() -> list[tuple[str, int, int, float]]:
    """(conversation_id, unscored_chunks, total_chunks, seconds) needing scores.

    A missing ``vad`` field does not compare equal to null in an aggregation, so
    the check is an explicit size test rather than ``$ne: null`` -- that mistake
    counts never-analyzed chunks as analyzed and hides exactly this population.
    """
    collection = AudioChunkDocument.get_pymongo_collection()
    rows = await collection.aggregate(
        [
            {"$match": {"deleted": {"$ne": True}}},
            {
                "$project": {
                    "conversation_id": 1,
                    "scored": {"$gt": [{"$size": {"$ifNull": ["$vad.scores", []]}}, 0]},
                    "duration": {"$ifNull": ["$duration", 10.0]},
                }
            },
            {
                "$group": {
                    "_id": "$conversation_id",
                    "total": {"$sum": 1},
                    "scored": {"$sum": {"$cond": ["$scored", 1, 0]}},
                    "seconds": {"$sum": "$duration"},
                }
            },
            {"$match": {"$expr": {"$lt": ["$scored", "$total"]}}},
            {"$sort": {"_id": 1}},
        ],
        allowDiskUse=True,
    ).to_list(length=None)
    return [
        (str(row["_id"]), row["total"] - row["scored"], row["total"], row["seconds"])
        for row in rows
        if row["_id"]
    ]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default: report)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://mongo:27017"))
    await init_beanie(
        database=client.chronicle,
        document_models=[Conversation, AudioChunkDocument, User],
    )

    pending = await unscored_conversations()
    if args.limit:
        pending = pending[: args.limit]
    unscored_chunks = sum(item[1] for item in pending)
    hours = sum(item[3] for item in pending) / 3600
    logger.info(
        "%d conversation(s) carry %d unscored chunk(s) across %.1f h of audio",
        len(pending),
        unscored_chunks,
        hours,
    )
    if not args.apply:
        logger.info("report only — nothing written")
        return

    # Conversations run concurrently: the cost is Opus decode, and the VAD
    # provider is instantiated per call because its state is per-stream, so
    # nothing is shared between them.
    semaphore = asyncio.Semaphore(args.concurrency)
    counters = {"done": 0, "scored": 0, "failed": 0}

    async def score(conversation_id: str) -> None:
        async with semaphore:
            try:
                result = await analyze_conversation_audio(conversation_id)
            except Exception as error:
                # One unreadable recording must not stop the sweep; it stays
                # unscored and is held back by the re-bound, the safe state.
                counters["failed"] += 1
                logger.warning(
                    "%s: %s: %s", conversation_id, type(error).__name__, error
                )
                return
            finally:
                counters["done"] += 1
                if counters["done"] % 25 == 0 or counters["done"] == len(pending):
                    logger.info(
                        "%d/%d conversations processed (%d failed)",
                        counters["done"],
                        len(pending),
                        counters["failed"],
                    )
            conversation = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            if conversation is not None:
                result["analyzed_at"] = datetime.now(timezone.utc)
                conversation.vad_analysis = Conversation.VadAnalysis(**result)
                await conversation.save()
            counters["scored"] += 1

    await asyncio.gather(*(score(item[0]) for item in pending))
    logger.info(
        "scored %d conversation(s), %d failed", counters["scored"], counters["failed"]
    )


if __name__ == "__main__":
    asyncio.run(main())
