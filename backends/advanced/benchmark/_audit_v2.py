"""Per-failure extraction-vs-retrieval audit for chronicle's 22 LongMemEval losses.

For each failed qid:
  1. Pull all ConvDoc / ConvChunk text from FalkorDB graph (chronicle_bench-<qid>).
  2. Substring-search ground-truth tokens against the vault → was the fact extracted?
  3. Call memory_service.search_memories(question, user_id, top_k=10) → did
     retrieval surface a doc containing the GT tokens?

Bucket:
  - VAULT_MISS  → extraction never wrote it (or wrote it stripped of key tokens)
  - RETRIEVAL_MISS → vault has it, search top-10 doesn't
  - SEARCH_HIT  → search top-10 has it; failure was the answering LLM not citing it
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from advanced_omi_backend.services.memory import get_memory_service

from benchmark.loader import load_longmemeval

RUNS = [
    "20260429-223203-197ca2",
    "20260430-002606-72a3f8",
    "20260430-005134-1e2e58",
    "20260430-011642-5dbaa1",
    "20260430-014422-83b55a",
]
ROOT = Path("/app/data/benchmark_runs")
STOPWORDS = {
    "the","a","an","and","or","of","to","in","on","at","by","for","with","my","i","is","was","were",
    "be","been","are","this","that","it","its","as","from","not","but","also","yes","no","ok",
    "minutes","seconds","minute","second","hours","hour","days","day","weeks","week","including",
    "last","you","your","previous","new","old","also","acceptable","mentioned","indicated",
}


def _tokenize(s: str) -> list[str]:
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-:.]+", s.lower())
    return [t for t in toks if t not in STOPWORDS and len(t) >= 3]


async def _vault_text(user_id: str) -> str:
    """Concat every ConvDoc markdown for this user via the on-disk vault."""
    svc = get_memory_service()
    if not svc._initialized:
        await svc.initialize()
    try:
        vault = svc.vault  # type: ignore[attr-defined]
        conv_ids = vault.list_docs(user_id)
        return "\n\n".join(vault.read_doc(user_id, cid) for cid in conv_ids)
    except Exception as e:
        print(f"  vault dump failed for {user_id}: {e}", file=sys.stderr)
        return ""


async def _search_topk_text(user_id: str, query: str, k: int = 10) -> str:
    svc = get_memory_service()
    if not svc._initialized:
        await svc.initialize()
    entries = await svc.search_memories(query=query, user_id=user_id, limit=k)
    return "\n".join(f"[{i+1}] {e.content}" for i, e in enumerate(entries[:k]))


def _bucket(vault: str, search_ctx: str, gt_toks: list[str], anchor: str | None) -> str:
    """Return VAULT_MISS / RETRIEVAL_MISS / SEARCH_HIT / WEAK based on token overlap."""
    if not gt_toks:
        return "WEAK"
    vault_lc = vault.lower()
    search_lc = search_ctx.lower()
    if anchor:
        a = anchor.lower()
        if a in search_lc:
            return "SEARCH_HIT"
        if a in vault_lc:
            return "RETRIEVAL_MISS"
        return "VAULT_MISS"
    # Fall back to majority-token overlap
    in_vault = sum(1 for t in gt_toks if t in vault_lc)
    in_search = sum(1 for t in gt_toks if t in search_lc)
    n = len(gt_toks)
    if in_search / n >= 0.5:
        return "SEARCH_HIT"
    if in_vault / n >= 0.5:
        return "RETRIEVAL_MISS"
    return "VAULT_MISS"


def _anchor(qid: str, gt: str) -> str | None:
    """Hand-picked anchor strings for the failures where token-overlap is too noisy.
    Empty/None falls back to majority-token bucketing."""
    return {
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
        "0a995998": None,  # numeric count, not a substring
        "6d550036": None,
        "gpt4_59c863d7": None,
        "e831120c": "3.5 weeks",
        "8a2466db": "premiere pro",
        "06878be2": "sony",
        "0edc2aef": "rooftop pool",
        "35a27287": "spanish",
        "gpt4_59149c77": None,
        "gpt4_f49edff3": None,
        "71017276": None,
        "gpt4_fa19884c": None,
    }.get(qid)


async def main() -> int:
    fails: dict[str, dict] = {}
    for run in RUNS:
        for line in (ROOT / run / "progress.jsonl").read_text().splitlines():
            r = json.loads(line)
            qid = r["question_id"]
            if r.get("status") == "done" and r.get("score") is False:
                fails[qid] = r

    # Load the matching dataset instances
    target = set(fails)
    found: dict[str, object] = {}
    for inst in load_longmemeval(variant="s", limit=500):
        if inst.question_id in target:
            found[inst.question_id] = inst
        if len(found) == len(target):
            break

    buckets: dict[str, list[str]] = {"VAULT_MISS": [], "RETRIEVAL_MISS": [], "SEARCH_HIT": [], "WEAK": []}

    for qid, fr in fails.items():
        inst = found.get(qid)
        if not inst:
            continue
        user_id = f"bench-{qid}"
        gt_toks = _tokenize(inst.answer)
        anchor = _anchor(qid, inst.answer)

        vault = await _vault_text(user_id)
        search_ctx = await _search_topk_text(user_id, inst.question, k=10)
        bucket = _bucket(vault, search_ctx, gt_toks, anchor)
        buckets[bucket].append(qid)

        a_show = anchor or f"toks={gt_toks[:5]}"
        print(f"{bucket:14s}  qid={qid:14s}  qtype={fr.get('question_type','?'):28s}  anchor={a_show!r}")

    print("\n=== Buckets ===")
    for b, qids in buckets.items():
        print(f"  {b:14s} {len(qids):2d}   {qids}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
