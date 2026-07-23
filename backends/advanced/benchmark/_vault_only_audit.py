"""Vault-only audit (no LLM calls): for each failure across the v3 runs,
check if the GT fact substring appears in the user's vault. Distinguishes
extraction failures (anchor not in vault) from retrieval/answer failures
(anchor in vault but the system still got it wrong).

Compatible with quota-exhausted state — no embeddings, no completions.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from advanced_omi_backend.services.memory import get_memory_service

from benchmark.loader import load_longmemeval

# v3 runs (WH-details + bullet-chunking + top_k=30)
RUNS = [
    "20260430-034630-13c3ac",  # SS-user n=20
    "20260430-040625-b458d4",  # multi-session n=4 (1 quota error)
    "20260430-040630-691187",  # knowledge-update n=5
    "20260430-040635-5c05a7",  # temporal-reasoning n=5
    "20260430-040640-cbedea",  # preference n=4 (1 quota error)
]
ROOT = Path("/app/data/benchmark_runs")

# anchors per qid (subset based on prior triage)
ANCHORS = {
    "58bf7951": "glass menagerie",
    "1e043500": "summer vibes",
    "58ef2f1c": "february 14",
    "f8c5f88b": "sports store",
    "5d3d2817": "marketing specialist",
    "ad7109d1": "500",
    "dccbc061": "atheist",
    "6a1eabeb": "25:50",
    "6aeb4375": "four",
    "945e3d21": "three times a week",
    "e831120c": "3.5 weeks",
    "8a2466db": "premiere pro",
    "06878be2": "sony",
    "0edc2aef": "rooftop pool",
    "35a27287": "spanish",
    "51a45a95": None,
}


async def vault_text(svc, user_id: str) -> str:
    if not svc._initialized:
        await svc.initialize()
    vault = svc.vault
    return "\n\n".join(vault.read_doc(user_id, cid) for cid in vault.list_docs(user_id))


async def main() -> int:
    svc = get_memory_service()

    fails: dict[str, dict] = {}
    for run in RUNS:
        path = ROOT / run / "progress.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            r = json.loads(line)
            qid = r["question_id"]
            if r.get("status") == "done" and r.get("score") is False:
                fails[qid] = r

    target = set(fails)
    if not target:
        print("No failures found across the listed run dirs (check run ids).")
        return 0

    found: dict[str, object] = {}
    for inst in load_longmemeval(variant="s", limit=500):
        if inst.question_id in target:
            found[inst.question_id] = inst
        if len(found) == len(target):
            break

    print(f"# {len(fails)} failures to audit (vault-only)\n")

    vault_hits = []
    vault_misses = []
    for qid, fr in fails.items():
        inst = found.get(qid)
        if not inst:
            print(f"  [missing instance for {qid}]")
            continue
        user_id = f"bench-{qid}"
        text = await vault_text(svc, user_id)
        anchor = ANCHORS.get(qid)
        if anchor is None:
            # Fall back: look for any meaningful word from GT
            words = [w for w in inst.answer.lower().split() if len(w) > 4]
            in_vault = any(w in text.lower() for w in words[:3])
            anchor_show = f"first-words={words[:3]}"
        else:
            in_vault = anchor in text.lower()
            anchor_show = repr(anchor)
        status = "VAULT_HIT (retrieval/answer fail)" if in_vault else "VAULT_MISS (extraction fail)"
        if in_vault:
            vault_hits.append(qid)
        else:
            vault_misses.append(qid)
        print(f"  {status:38s}  qid={qid:14s}  qtype={fr.get('question_type','?'):28s}  anchor={anchor_show}")

    print(f"\n=== Buckets ===")
    print(f"  VAULT_HIT (retrieval/answer fail): {len(vault_hits):2d}   {vault_hits}")
    print(f"  VAULT_MISS (extraction fail):     {len(vault_misses):2d}   {vault_misses}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
