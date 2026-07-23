"""Phase A ingestion harness for the LongMemEval benchmark.

Drives Chronicle's existing chat path with backdated turns:

- ``ingest_chat_session`` — writes a session of user/assistant turns and runs
  synchronous memory + KG extraction. Equivalent end-state to a real chat
  session, but blocking (no RQ) so the benchmark runner can sequence work.
- ``cleanup_user`` — wipes Mongo chat data, the configured memory provider's
  storage, and KG entities for a benchmark user_id. Idempotent.

The harness is provider-agnostic: whatever ``MEMORY_PROVIDER`` Chronicle is
configured with handles the memory side via ``delete_all_user_memories``.
KG cleanup is added on top because the KG service is independent of the
memory provider — ``:Entity`` / ``:Conversation`` nodes are managed by
``KnowledgeGraphService`` regardless of which provider is active.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Literal, TypedDict
from uuid import uuid4

from advanced_omi_backend.chat_service import ChatMessage, get_chat_service
from advanced_omi_backend.services.knowledge_graph import get_knowledge_graph_service
from advanced_omi_backend.services.memory import get_memory_service

logger = logging.getLogger(__name__)


class Turn(TypedDict):
    role: Literal["user", "assistant"]
    content: str


async def ingest_chat_session(
    user_id: str,
    turns: list[Turn],
    session_date: datetime,
) -> tuple[str, int, bool]:
    """Ingest one chat session and run synchronous memory extraction.

    Args:
        user_id: User identifier (any string — benchmark uses ``bench-<question_id>``).
        turns: Ordered user/assistant turns making up the session.
        session_date: Tz-aware UTC datetime to backdate the session to. Becomes
            the timestamp on every persisted message and is prefixed onto every
            turn so the LLM extractor sees per-turn temporal grounding.

    Returns:
        ``(session_id, memory_count, success)``. The runner uses
        ``memory_count == 0`` as an LLM-side extraction-failure signal —
        LongMemEval sessions are never legitimately empty.
    """
    if session_date.tzinfo is None:
        raise ValueError("session_date must be tz-aware (UTC)")

    t_start = time.perf_counter()

    chat = get_chat_service()
    t0 = time.perf_counter()
    session = await chat.create_session(
        user_id=user_id,
        title=f"bench-{session_date.date().isoformat()}",
    )
    t_create = time.perf_counter() - t0

    prefix = f"[{session_date.strftime('%Y-%m-%d %H:%M')}] "

    t0 = time.perf_counter()
    for turn in turns:
        msg = ChatMessage(
            message_id=str(uuid4()),
            session_id=session.session_id,
            user_id=user_id,
            role=turn["role"],
            content=prefix + turn["content"],
            timestamp=session_date,
        )
        ok = await chat.add_message(msg)
        if not ok:
            raise RuntimeError(
                f"add_message failed for session {session.session_id} (user {user_id})"
            )
    t_msgs = time.perf_counter() - t0

    t0 = time.perf_counter()
    success, _memory_ids, count = await chat.extract_memories_from_session(
        session_id=session.session_id, user_id=user_id
    )
    t_extract = time.perf_counter() - t0

    t_total = time.perf_counter() - t_start
    logger.info(
        "ingest_chat_session: user=%s session=%s turns=%d memories=%d success=%s | "
        "create=%.3fs add_msgs=%.3fs extract=%.2fs total=%.2fs",
        user_id,
        session.session_id,
        len(turns),
        count,
        success,
        t_create,
        t_msgs,
        t_extract,
        t_total,
    )
    return session.session_id, count, success


async def cleanup_user(user_id: str) -> None:
    """Idempotently wipe all storage layers for a benchmark user.

    Layers:
      1. MongoDB chat_sessions + chat_messages (per-session via ChatService).
      2. Memory provider — whatever ``MEMORY_PROVIDER`` Chronicle is using
         handles its own storage via ``delete_all_user_memories``.
      3. FalkorDB KG: ``:Entity`` nodes (and ``:Conversation``, which carries
         ``:Entity`` too) scoped by ``user_id``. DETACH DELETE removes incident
         relationships in the same step.

    Safe to call against an already-empty user — every layer reports zero
    deletions without raising.
    """
    chat = get_chat_service()
    sessions = await chat.get_user_sessions(user_id, limit=10_000)
    for s in sessions:
        await chat.delete_session(s.session_id, user_id)
    logger.info(
        "cleanup_user: removed %d chat sessions for %s", len(sessions), user_id
    )

    memory_service = get_memory_service()
    deleted_memories = await memory_service.delete_all_user_memories(user_id)
    logger.info(
        "cleanup_user: memory_service.delete_all_user_memories(%s) -> %d",
        user_id,
        deleted_memories,
    )

    kg = get_knowledge_graph_service()
    deleted_kg = await kg.delete_all_user_entities(user_id)
    logger.info("cleanup_user: KG entities deleted=%d for %s", deleted_kg, user_id)
