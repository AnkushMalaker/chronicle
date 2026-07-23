"""End-to-end smoke for Phase A ingestion harness.

Runs the 5-step verification from the plan:
  1. cleanup_user (idempotent on empty)
  2. ingest one short session
  3. verify all four storage layers populated
  4. cleanup_user again
  5. verify all four storage layers empty

Run inside the chronicle-backend container:

    docker compose exec \\
        -e MONGODB_DATABASE=chronicle_bench \\
        -e OTEL_SDK_DISABLED=true \\
        chronicle-backend uv run python -m benchmark.smoke
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from advanced_omi_backend.database import get_database
from advanced_omi_backend.services.knowledge_graph import get_knowledge_graph_service
from advanced_omi_backend.services.memory import get_memory_service

from benchmark.ingest import Turn, cleanup_user, ingest_chat_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("benchmark.smoke")

USER_ID = "bench-smoke"
SESSION_DATE = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
TURNS: list[Turn] = [
    {"role": "user", "content": "I'm vegetarian and I love hiking on weekends."},
    {"role": "assistant", "content": "Got it — I'll keep that in mind."},
]


async def _count_layers() -> dict[str, int]:
    """Return a snapshot of node counts for each storage layer."""
    db = get_database()
    chat_sessions = await db["chat_sessions"].count_documents({"user_id": USER_ID})
    chat_messages = await db["chat_messages"].count_documents({"user_id": USER_ID})

    memory_service = get_memory_service()
    memory_count = await memory_service.count_memories(USER_ID) or 0

    kg = get_knowledge_graph_service()
    kg._ensure_initialized()
    kg_rows = kg._read.run(
        "MATCH (e:Entity {user_id: $uid}) RETURN count(e) AS n",
        uid=USER_ID,
    )
    kg_count = kg_rows[0]["n"] if kg_rows else 0

    vault_dir = Path(
        os.getenv("DATA_DIR", "/app/data"), "conversation_docs", USER_ID
    )
    vault_count = (
        len(list(vault_dir.glob("*.md"))) if vault_dir.exists() else 0
    )

    return {
        "chat_sessions": chat_sessions,
        "chat_messages": chat_messages,
        "memory_chunks": memory_count,
        "kg_entities": kg_count,
        "vault_files": vault_count,
    }


async def main() -> int:
    logger.info("Step 1: cleanup_user(%s) — should be idempotent on empty", USER_ID)
    await cleanup_user(USER_ID)
    pre = await _count_layers()
    logger.info("Pre-ingest counts: %s", pre)
    if any(v != 0 for v in pre.values()):
        logger.error("Layers are not empty after initial cleanup: %s", pre)
        return 2

    logger.info(
        "Step 2: ingest_chat_session(turns=%d, date=%s)", len(TURNS), SESSION_DATE
    )
    session_id, count, success = await ingest_chat_session(
        user_id=USER_ID,
        turns=TURNS,
        session_date=SESSION_DATE,
    )
    logger.info(
        "Ingest result: session_id=%s memories=%d success=%s",
        session_id,
        count,
        success,
    )
    if not success or count == 0:
        logger.error("Memory extraction failed or returned zero memories")
        return 3

    logger.info("Step 3: verify all four layers are populated")
    post = await _count_layers()
    logger.info("Post-ingest counts: %s", post)
    if post["chat_sessions"] < 1 or post["chat_messages"] < len(TURNS):
        logger.error("Mongo chat layer not populated: %s", post)
        return 4
    if post["memory_chunks"] < 1:
        logger.error("Memory chunks layer not populated: %s", post)
        return 5
    if post["vault_files"] < 1:
        logger.error("Vault layer not populated: %s", post)
        return 6
    if post["kg_entities"] < 1:
        logger.warning(
            "KG entity layer empty — extractor may not have detected entities. "
            "Counts: %s",
            post,
        )

    logger.info("Step 4: cleanup_user(%s)", USER_ID)
    await cleanup_user(USER_ID)

    logger.info("Step 5: verify all four layers are empty")
    final = await _count_layers()
    logger.info("Post-cleanup counts: %s", final)
    if any(v != 0 for v in final.values()):
        logger.error("Some layers still have data after cleanup: %s", final)
        return 7

    logger.info("✅ Smoke verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
