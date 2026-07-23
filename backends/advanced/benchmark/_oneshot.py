"""One-shot ingestion + retrieval + judge for a single LongMemEval question.

Usage: python -m benchmark._oneshot <question_id>

Lets us re-test retrieval after a code change without re-running the full
runner orchestration. Each call wipes and re-creates the bench user's data.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmark.ingest import cleanup_user, ingest_chat_session
from benchmark.judge import JudgeCache, judge_answer
from benchmark.retrieve import build_answer_prompt, retrieve_context

from advanced_omi_backend.llm_client import get_llm_client
from advanced_omi_backend.services.memory import get_memory_service


DATASET_PATH = (
    "/root/.cache/huggingface/hub/datasets--xiaowu0162--longmemeval-cleaned/"
    "snapshots/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json"
)


def _print_answer_source(row: dict) -> None:
    session_ids = row.get("haystack_session_ids") or []
    answer_ids = set(row["answer_session_ids"])
    for i, session_id in enumerate(session_ids):
        if session_id not in answer_ids:
            continue
        print(
            f"\n=== RAW ANSWER SESSION {session_id} "
            f"(index {i}, date {row['haystack_dates'][i]}) ===",
            flush=True,
        )
        for turn in row["haystack_sessions"][i]:
            marker = " <-- HAS ANSWER" if turn.get("has_answer") else ""
            print(f"[{turn['role']}]{marker} {turn['content']}\n", flush=True)


async def _print_extracted_answer_chunks(user_id: str, conversation_ids: list[str]) -> None:
    if not conversation_ids:
        return
    memory_service = get_memory_service()
    if not memory_service._initialized:
        await memory_service.initialize()
    # Chronicle exposes low-level graph IO helpers used by this debug dump.
    # Other providers (for example Graphiti) do not, so skip this optional step.
    if not hasattr(memory_service, "_get_io"):
        print(
            "\n=== EXTRACTED CHUNKS DEBUG SKIPPED (provider has no _get_io) ===",
            flush=True,
        )
        return
    _, read, _ = memory_service._get_io(user_id)
    for conversation_id in conversation_ids:
        rows = read.run(
            """
            MATCH (c:ConvChunk)
            WHERE c.conversation_id = $conversation_id
            RETURN c.id AS id, c.section_title AS section, c.text AS text
            ORDER BY c.id
            """,
            conversation_id=conversation_id,
        )
        print(f"\n=== EXTRACTED CHUNKS FOR {conversation_id} ===", flush=True)
        for row in rows:
            print(f"\n--- {row['section']} ({row['id']}) ---\n{row['text']}", flush=True)


def _parse_cli_args(argv: list[str]) -> tuple[str, bool]:
    qid = "5d3d2817"
    resume = False
    for arg in argv[1:]:
        if arg in {"--resume", "--skip-ingest"}:
            resume = True
            continue
        if arg.startswith("-"):
            raise SystemExit(f"Unknown option: {arg}")
        qid = arg
    return qid, resume


async def main(qid: str, resume: bool = False) -> int:
    with open(DATASET_PATH) as f:
        data = json.load(f)
    row = next(r for r in data if r["question_id"] == qid)

    print(f"QID: {qid}")
    print(f"QUESTION: {row['question']}")
    print(f"GROUND TRUTH: {row['answer']}")
    print(f"NUM SESSIONS: {len(row['haystack_sessions'])}")
    print(f"ANSWER SESSION: {row['answer_session_ids']}")
    print(flush=True)
    _print_answer_source(row)

    user = f"bench-{qid}"
    answer_conversation_ids: list[str] = []
    if resume:
        print(
            f"\n=== RESUME MODE: reusing existing user data for {user}; "
            "skipping cleanup + ingestion ===\n",
            flush=True,
        )
    else:
        await cleanup_user(user)
        print(f"cleaned user {user}", flush=True)

        t_start = time.perf_counter()
        n_mem_total = 0
        sessions = row["haystack_sessions"]
        session_ids = row.get("haystack_session_ids") or []
        answer_session_ids = set(row["answer_session_ids"])
        dates = row.get("haystack_dates", [])
        for i, sess in enumerate(sessions):
            if i < len(dates):
                try:
                    ts = datetime.fromisoformat(dates[i].replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)
            else:
                ts = datetime.now(timezone.utc)
            turns = [{"role": t["role"], "content": t["content"]} for t in sess]
            conversation_id, count, _ = await ingest_chat_session(user, turns, ts)
            if i < len(session_ids) and session_ids[i] in answer_session_ids:
                answer_conversation_ids.append(conversation_id)
            n_mem_total += count
            if (i + 1) % 10 == 0:
                print(
                    f"  ... {i + 1}/{len(sessions)} sessions, {n_mem_total} memories, "
                    f"elapsed {time.perf_counter() - t_start:.1f}s",
                    flush=True,
                )
        t_ingest = time.perf_counter() - t_start
        print(
            f"\n=== INGEST DONE in {t_ingest:.1f}s, {n_mem_total} memories across "
            f"{len(sessions)} sessions ===\n",
            flush=True,
        )
    await _print_extracted_answer_chunks(user, answer_conversation_ids)

    ctx = await retrieve_context(row["question"], user)
    print(f"=== RETRIEVED CONTEXT (first 1500 chars) ===\n{ctx[:1500]}\n", flush=True)
    prompt = build_answer_prompt(row["question"], ctx, None)
    answer = get_llm_client().generate(prompt=prompt)
    print(f"=== ANSWER ===\n{answer}\n", flush=True)

    cache = JudgeCache(Path(tempfile.mkdtemp()))
    score, raw, model = judge_answer(
        question_id=qid,
        question_type=row["question_type"],
        question=row["question"],
        ground_truth=row["answer"],
        answer=answer,
        abstention=False,
        cache=cache,
    )
    print(
        f"=== JUDGE ===\nscore={score}  judge_model={model}  raw={raw!r}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    qid, resume_mode = _parse_cli_args(sys.argv)
    sys.exit(asyncio.run(main(qid, resume=resume_mode)))
