"""Re-run promotion for conversational episodes whose cited recordings went stale.

Promotion runs once, when a timeline generation is published. Anything that replaces a
conversation *afterwards* — a dedup sweep, a merge, a re-bound — leaves the episode
citing a soft-deleted id, and the meeting it identified silently stays hidden. New
generations self-heal now that promotion resolves references (see
``services/timeline/recording_refs.py``), but episodes already published do not.

    uv run python scripts/repromote_conversational_episodes.py            # dry run
    uv run python scripts/repromote_conversational_episodes.py --apply
"""

import argparse
import asyncio
import logging
import os
from collections import Counter

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import TimelineEpisode
from advanced_omi_backend.services.timeline.discovery import _cited_conversation_ids
from advanced_omi_backend.services.timeline.recording_refs import (
    resolve_live_recordings,
)

logger = logging.getLogger("repromote")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://mongo:27017"))
    await init_beanie(
        database=client.chronicle,
        document_models=[Conversation, AudioChunkDocument, TimelineEpisode],
    )

    episodes = await TimelineEpisode.find(
        {"conversational": True, "status": {"$ne": "superseded"}}
    ).to_list()
    logger.info("%d conversational episode(s)", len(episodes))

    outcome: Counter = Counter()
    promote: set[str] = set()
    for episode in episodes:
        cited = _cited_conversation_ids(episode)
        if not cited:
            outcome["cites nothing"] += 1
            continue
        resolved = await resolve_live_recordings(cited)
        if not resolved:
            outcome["cites nothing live, unrecoverable"] += 1
            continue
        outcome["resolved" if resolved != cited else "already live"] += 1
        promote |= resolved

    for reason, count in outcome.most_common():
        logger.info("  %-34s %d", reason, count)

    collection = Conversation.get_pymongo_collection()
    hidden = [
        document["conversation_id"]
        async for document in collection.find(
            {
                "conversation_id": {"$in": sorted(promote)},
                "data_purpose": "capture_evidence",
                "deleted": {"$ne": True},
            },
            {"conversation_id": 1},
        )
    ]
    logger.info(
        "\n%d recording(s) resolved, %d of them still hidden", len(promote), len(hidden)
    )
    for conversation_id in hidden:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        logger.info(
            "  %s  %s  %5.1fm  %s",
            conversation_id[:8],
            conversation.created_at.strftime("%m-%d %H:%M"),
            (conversation.audio_total_duration or 0) / 60,
            (conversation.title or "")[:44],
        )

    if not args.apply:
        logger.info("\ndry run — nothing written")
        return

    if hidden:
        await collection.update_many(
            {"conversation_id": {"$in": hidden}},
            {
                "$set": {
                    "data_purpose": "conversation",
                    "memory_excluded": False,
                    "memory_exclusion_reason": None,
                }
            },
        )
    logger.info("\npromoted %d recording(s)", len(hidden))


if __name__ == "__main__":
    asyncio.run(main())
