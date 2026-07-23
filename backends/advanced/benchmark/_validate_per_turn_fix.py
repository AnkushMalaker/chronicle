"""Validate the per-turn graphiti ingestion fix on the existing bench user.

For LongMemEval QID 5d3d2817, the answer session is one of 53 ingested
sessions for ``bench-5d3d2817``. Old code stored that session as a single
big-message episode and the LLM missed the "previous role: marketing
specialist" fact. Per-turn ingestion now lives in
``GraphitiMemoryService.add_memory``.

This script:
  1. Finds the answer session in MongoDB by content match.
  2. Re-runs ``chat.extract_memories_from_session`` for that session, which
     internally goes through the new per-turn ingestion path with
     ``allow_update=True`` so the old facts/episode are removed first.
  3. Lists the resulting facts in the bench graph that match
     marketing/specialist/previous/startup keywords.
  4. Runs retrieve + judge to confirm the question now scores True.

We deliberately re-ingest only the answer session, not all 53 sessions —
the goal is to validate the fix end-to-end without paying for a full re-
ingest. If retrieval still fails, we know it's a ranking/scoring issue,
not extraction.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from benchmark.judge import JudgeCache, judge_answer
from benchmark.retrieve import build_answer_prompt, retrieve_context

from advanced_omi_backend.chat_service import get_chat_service
from advanced_omi_backend.llm_client import get_llm_client
from advanced_omi_backend.services.memory import get_memory_service


DATASET_PATH = (
    "/root/.cache/huggingface/hub/datasets--xiaowu0162--longmemeval-cleaned/"
    "snapshots/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json"
)
QID = "5d3d2817"
USER_ID = f"bench-{QID}"
TARGET_SUBSTRING = "marketing specialist at a small startup"


async def _find_answer_session_id() -> str:
    chat = get_chat_service()
    if not chat._initialized:
        await chat.initialize()
    sessions = await chat.get_user_sessions(USER_ID, limit=10_000)
    if not sessions:
        raise SystemExit(f"No chat sessions for {USER_ID}; did you run ingest?")
    for s in sessions:
        msgs = await chat.get_session_messages(s.session_id, USER_ID)
        for m in msgs:
            if TARGET_SUBSTRING in m.content:
                return s.session_id
    raise SystemExit(
        f"No session containing target text found for {USER_ID}. "
        "Run the full benchmark ingest first."
    )


async def _list_relevant_facts() -> list[str]:
    svc = get_memory_service()
    if not svc._initialized:
        await svc.initialize()
    memories = await svc.get_all_memories(USER_ID, limit=2000)
    matches = []
    for m in memories:
        text = (m.content or "").lower()
        if any(kw in text for kw in ("marketing", "specialist", "previous", "startup")):
            matches.append(m.content)
    return matches


async def main() -> int:
    with open(DATASET_PATH) as f:
        data = json.load(f)
    row = next(r for r in data if r["question_id"] == QID)
    print(f"QUESTION: {row['question']}")
    print(f"GROUND TRUTH: {row['answer']}\n", flush=True)

    session_id = await _find_answer_session_id()
    print(f"Answer chat session_id: {session_id}", flush=True)

    print("\n--- Relevant facts BEFORE re-ingest ---", flush=True)
    for f in await _list_relevant_facts():
        print(f"  - {f}", flush=True)

    chat = get_chat_service()
    print("\n--- Re-ingesting answer session via per-turn path ---", flush=True)
    success, _, count = await chat.extract_memories_from_session(
        session_id=session_id, user_id=USER_ID
    )
    print(
        f"extract_memories_from_session: success={success} count={count}",
        flush=True,
    )

    print("\n--- Relevant facts AFTER re-ingest ---", flush=True)
    for f in await _list_relevant_facts():
        print(f"  - {f}", flush=True)

    ctx = await retrieve_context(row["question"], USER_ID)
    print(f"\n--- RETRIEVED CONTEXT (first 1500) ---\n{ctx[:1500]}", flush=True)
    prompt = build_answer_prompt(row["question"], ctx, None)
    answer = get_llm_client().generate(prompt=prompt)
    print(f"\n--- ANSWER ---\n{answer}", flush=True)

    cache = JudgeCache(Path(tempfile.mkdtemp()))
    score, raw, model = judge_answer(
        question_id=QID,
        question_type=row["question_type"],
        question=row["question"],
        ground_truth=row["answer"],
        answer=answer,
        abstention=False,
        cache=cache,
    )
    print(
        f"\n--- JUDGE ---\nscore={score}  raw={raw!r}  model={model}",
        flush=True,
    )
    return 0 if score else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
