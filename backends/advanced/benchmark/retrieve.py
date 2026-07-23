"""Retrieval for the benchmark answerer.

Calls ``MemoryServiceBase.search_memories`` on whatever provider Chronicle
is configured with and renders the results as a flat context block. Any
provider that satisfies the base interface plugs in unchanged.
"""

from __future__ import annotations

import logging

from advanced_omi_backend.services.memory import get_memory_service

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 30


async def retrieve_context(query: str, user_id: str, top_k: int = DEFAULT_TOP_K) -> str:
    """Return a textual context block for ``query`` scoped to ``user_id``.

    Empty string if nothing is found (the answerer prompt handles that).
    """
    memory_service = get_memory_service()
    memories = await memory_service.search_memories(
        query=query, user_id=user_id, limit=top_k
    )
    if not memories:
        logger.info("retrieve_context: no memories for user=%s", user_id)
        return ""

    lines = ["# Relevant Personal Memories"]
    for i, mem in enumerate(memories, 1):
        if not mem.content:
            continue
        lines.append(f"{i}. {mem.content}")
    return "\n".join(lines)


ANSWER_SYSTEM_PROMPT = (
    "You are answering a question about a user based on their personal "
    "memory database. Use only the memories provided as context. If the "
    "memories don't contain enough information to answer, say so plainly "
    "rather than guessing. Be concise and direct — do not pad with caveats."
)


def build_answer_prompt(question: str, context: str, question_date: str | None) -> str:
    """Compose the full prompt for the LLM that produces the candidate answer."""
    parts: list[str] = []
    if question_date:
        parts.append(f"# Today's date\n{question_date}\n")
    if context:
        parts.append(context)
    parts.append("# Question")
    parts.append(question)
    parts.append("\n# Your answer")
    return "\n".join(parts)
