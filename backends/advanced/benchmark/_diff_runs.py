"""Compare two runs side-by-side: per-qid GT vs old answer vs new answer."""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from benchmark.loader import load_longmemeval

OLD = Path("/app/data/benchmark_runs/20260429-223203-197ca2")
NEW = Path("/app/data/benchmark_runs/20260430-030423-b09534")

QIDS = ["66f24dbb", "6f9b354f", "7527f7e2", "af8d2e46", "e47becba",
        "58ef2f1c"]  # 5 regressions + 1 still-failing


def load(run: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in (run / "progress.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("status") == "done":
            out[r["question_id"]] = r
    return out


def main() -> int:
    old = load(OLD)
    new = load(NEW)
    target = set(QIDS)
    found: dict[str, object] = {}
    for inst in load_longmemeval(variant="s", limit=500):
        if inst.question_id in target:
            found[inst.question_id] = inst
        if len(found) == len(target):
            break

    for qid in QIDS:
        inst = found.get(qid)
        if not inst:
            continue
        print(f"\n{'='*70}\nqid={qid}  qtype={inst.question_type}")
        print(f"Q : {inst.question}")
        print(f"GT: {inst.answer!r}")
        print(f"--- OLD (score={old.get(qid, {}).get('score')}):")
        print(f"  {old.get(qid, {}).get('answer','')[:400]}")
        print(f"--- NEW (score={new.get(qid, {}).get('score')}):")
        print(f"  {new.get(qid, {}).get('answer','')[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
