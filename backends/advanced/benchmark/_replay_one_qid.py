"""Re-ingest one specific LongMemEval instance, retrieve, answer, and judge.

Bypasses the run_longmemeval CLI's "first N" pagination so we can hit a
specific qid for prompt iteration without re-running everything before it.

Usage (inside chronicle-backend container):
    python -m benchmark._replay_one_qid 58bf7951
"""

from __future__ import annotations

import asyncio
import logging
import sys

from advanced_omi_backend.llm_client import get_llm_client

from benchmark.ingest import cleanup_user, ingest_chat_session
from benchmark.judge import judge_answer, resolve_judge_model
from benchmark.loader import load_longmemeval
from benchmark.progress import JudgeCache, open_run
from benchmark.retrieve import build_answer_prompt, retrieve_context

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
)
logger = logging.getLogger("rs.replay")


async def main(qid: str) -> int:
    instance = next(
        (
            inst
            for inst in load_longmemeval(variant="s", limit=20)
            if inst.question_id == qid
        ),
        None,
    )
    if instance is None:
        print(f"qid={qid} not in first 20 LongMemEval_S instances")
        return 1

    user_id = f"bench-{qid}"
    print(f"# Question: {instance.question}")
    print(f"# Ground truth: {instance.answer}")
    print(f"# Sessions: {len(instance.sessions)}  qtype: {instance.question_type}")
    print()

    # Fresh ingest
    await cleanup_user(user_id)
    sorted_sessions = sorted(instance.sessions, key=lambda s: s.date)
    total = 0
    for session in sorted_sessions:
        if not session.turns:
            continue
        _, count, _ok = await ingest_chat_session(
            user_id=user_id, turns=session.turns, session_date=session.date
        )
        total += count
    print(f"# Ingested {len(sorted_sessions)} sessions, {total} memories")

    # Retrieve + answer
    qdate = instance.question_date.isoformat() if instance.question_date else None
    ctx = await retrieve_context(query=instance.question, user_id=user_id)
    prompt = build_answer_prompt(question=instance.question, context=ctx, question_date=qdate)
    answer = get_llm_client().generate(prompt=prompt).strip()
    print(f"\n# Answer\n{answer}\n")

    # Judge — use a fresh in-memory-only progress dir to avoid touching shared cache
    _rid, _rdir, _progress, _cache = open_run(None, runs_root=__import__("pathlib").Path("/tmp/rs_replay"))
    label, raw, model = judge_answer(
        question_id=qid,
        question_type=instance.question_type,
        question=instance.question,
        ground_truth=instance.answer,
        answer=answer,
        abstention=instance.is_abstention,
        cache=_cache,
    )
    print(f"# Judge: model={model}  label={label}  raw={raw!r}")
    return 0 if label else 2


if __name__ == "__main__":
    qid = sys.argv[1] if len(sys.argv) > 1 else "58bf7951"
    sys.exit(asyncio.run(main(qid)))
