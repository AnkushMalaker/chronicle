"""1-session smoke for the rolling_summary memory provider.

Bypasses the global memory-service singleton (which reads config.yml) so we
can exercise rolling_summary without flipping the running app's provider.
After this passes, switch ``config.yml`` ``memory.provider: rolling_summary``
and run the LongMemEval harness.

Usage (from chronicle-backend container):

    docker compose exec \
        -e MONGODB_DATABASE=chronicle_bench \
        -e OTEL_SDK_DISABLED=true \
        chronicle-backend uv run python -m benchmark._smoke_rolling_summary
"""

from __future__ import annotations

import asyncio
import logging
import sys

from advanced_omi_backend.database import get_database
from advanced_omi_backend.services.memory.config import build_memory_config_from_env
from advanced_omi_backend.services.memory.providers.rolling_summary import (
    COLLECTION_NAME,
    RollingSummaryMemoryService,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
)
logger = logging.getLogger("rs.smoke")

USER_ID = "bench-rs-smoke"
USER_EMAIL = "rs-smoke@example.com"
SOURCE_ID = "rs-smoke-session-1"
CLIENT_ID = "rs_smoke"

# A realistic-ish single session — user shares a few stable facts and one event.
TRANSCRIPT = """\
[2024-01-15 12:00] User: Hey, I've been getting back into running. I joined the Bangalore Runners Club last weekend.
[2024-01-15 12:00] Assistant: Nice! Are you training for anything specific?
[2024-01-15 12:00] User: Yeah, I'm planning to run the TCS World 10K in May 2024. Also, I'm vegetarian — does that affect what I should eat before a race?
[2024-01-15 12:00] Assistant: Vegetarian runners do great. You'll want carbs the night before — pasta, rice, lentils. What's your current weekly mileage?
[2024-01-15 12:00] User: About 25 km a week. I work as a backend engineer at Acme Corp so I run before work, around 6 am.
[2024-01-15 12:00] Assistant: That's a solid base. Stick with that and add one long run on Sunday — 8 to 10 km — and you'll be ready by May.
"""


async def _doc_for(user_id: str):
    db = get_database()
    return await db[COLLECTION_NAME].find_one({"user_id": user_id})


async def main() -> int:
    cfg = build_memory_config_from_env()
    # Override provider just for this smoke — bypasses the singleton entirely.
    svc = RollingSummaryMemoryService(cfg)
    await svc.initialize()

    # Idempotent reset
    deleted = await svc.delete_all_user_memories(USER_ID)
    logger.info("Pre-smoke cleanup deleted %d docs for %s", deleted, USER_ID)
    pre = await _doc_for(USER_ID)
    if pre is not None:
        logger.error("Layer not empty after cleanup: %s", pre)
        return 2

    # Ingest one session
    success, fact_ids = await svc.add_memory(
        transcript=TRANSCRIPT,
        client_id=CLIENT_ID,
        source_id=SOURCE_ID,
        user_id=USER_ID,
        user_email=USER_EMAIL,
        allow_update=False,
    )
    logger.info("add_memory: success=%s facts=%d", success, len(fact_ids))
    if not success or len(fact_ids) == 0:
        logger.error("add_memory failed or produced 0 facts")
        return 3

    doc = await _doc_for(USER_ID)
    if doc is None:
        logger.error("Mongo doc missing after add_memory")
        return 4
    profile = (doc.get("user_profile") or "").strip()
    summary = (doc.get("rolling_summary") or "").strip()
    logger.info(
        "doc state: fact_count=%d profile_len=%d summary_len=%d tokens_est=%s",
        doc.get("fact_count", -1),
        len(profile),
        len(summary),
        doc.get("summary_tokens_est"),
    )
    print("\n=== USER PROFILE ===\n" + (profile or "(empty)") + "\n")
    print("=== ROLLING SUMMARY ===\n" + (summary or "(empty)") + "\n")

    if not profile and not summary:
        logger.error("Neither profile nor summary populated — extractor produced nothing")
        return 5

    # Retrieval — query is intentionally ignored by the provider.
    entries = await svc.search_memories(
        query="What is the user training for?", user_id=USER_ID, limit=10
    )
    logger.info(
        "search_memories returned %d entries (kinds=%s)",
        len(entries),
        [e.metadata.get("kind") for e in entries],
    )
    if not entries:
        logger.error("search_memories returned empty — retrieval is broken")
        return 6

    # count_memories
    cnt = await svc.count_memories(USER_ID)
    logger.info("count_memories: %s", cnt)

    # Final cleanup
    deleted = await svc.delete_all_user_memories(USER_ID)
    logger.info("Post-smoke cleanup deleted %d docs", deleted)
    if await _doc_for(USER_ID) is not None:
        logger.error("Cleanup did not remove user doc")
        return 7

    logger.info("✅ rolling_summary 1-session smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
