"""Re-run retrieve+answer+judge against an existing run's user graphs at a
new top_k, without re-ingest. Tests whether bumping retrieval depth recovers
the regressions the WH-details prompt introduced.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from advanced_omi_backend.llm_client import get_llm_client

from benchmark.judge import judge_answer
from benchmark.loader import load_longmemeval
from benchmark.progress import JudgeCache
from benchmark.retrieve import build_answer_prompt, retrieve_context


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--runs-dir", default="/app/data/benchmark_runs")
    p.add_argument("--top-k", type=int, default=30)
    args = p.parse_args()

    run_dir = Path(args.runs_dir) / args.run_id
    progress_path = run_dir / "progress.jsonl"
    # Latest entry per qid
    latest: dict[str, dict] = {}
    for line in progress_path.read_text().splitlines():
        r = json.loads(line)
        latest[r["question_id"]] = r

    qids = [qid for qid, r in latest.items() if r.get("status") == "done"]
    print(f"# {len(qids)} done qids in {args.run_id}; reanswering at top_k={args.top_k}")

    # Stream the dataset to find matching instances
    target = set(qids)
    found: dict[str, object] = {}
    for inst in load_longmemeval(variant="s", limit=500):
        if inst.question_id in target:
            found[inst.question_id] = inst
        if len(found) == len(target):
            break

    # Use a separate judge cache so we don't conflate
    cache_dir = run_dir.parent / f"_reanswer_{args.run_id}_topk{args.top_k}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = JudgeCache(cache_dir)

    client = get_llm_client()
    correct = 0
    flips_to_true: list[tuple[str, str, str]] = []
    flips_to_false: list[tuple[str, str, str]] = []

    for qid in qids:
        inst = found.get(qid)
        if not inst:
            continue
        user_id = f"bench-{qid}"
        qdate = inst.question_date.isoformat() if inst.question_date else None
        ctx = await retrieve_context(query=inst.question, user_id=user_id, top_k=args.top_k)
        prompt = build_answer_prompt(question=inst.question, context=ctx, question_date=qdate)
        answer = client.generate(prompt=prompt).strip()
        label, _raw, _model = judge_answer(
            question_id=qid,
            question_type=inst.question_type,
            question=inst.question,
            ground_truth=inst.answer,
            answer=answer,
            abstention=inst.is_abstention,
            cache=cache,
        )
        if label:
            correct += 1
        old = latest[qid].get("score")
        if old is False and label is True:
            flips_to_true.append((qid, inst.answer, answer))
        elif old is True and label is False:
            flips_to_false.append((qid, inst.answer, answer))
        print(f"  qid={qid:14s} old={old}  new={label}")

    print(f"\nReanswered {len(qids)} -> {correct} correct ({correct/len(qids):.4f})")
    print(f"F->T: {len(flips_to_true)}    T->F: {len(flips_to_false)}")
    for qid, gt, ans in flips_to_false:
        print(f"\n  REGRESSION qid={qid}\n    GT: {gt!r}\n    new: {ans[:200]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
