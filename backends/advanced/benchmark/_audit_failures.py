"""For each failing qid, compute three things:

1. Was the needle present in the haystack? (search the raw transcripts of
   the answer_session_ids for an obvious anchor string from ground truth.)
2. Did extraction preserve it into user_profile or rolling_summary?
3. What did our system answer?

The goal is to attribute each failure to one of:
  A) needle missing from the haystack (LongMemEval data quirk)
  B) extraction dropped it (our prompt)
  C) retrieval / answering LLM ignored present evidence
"""

from __future__ import annotations

import asyncio
import sys

from advanced_omi_backend.database import get_database

from benchmark.loader import load_longmemeval


# (qid, anchor_strings_to_grep_in_transcript, our_answer_summary)
TARGETS = [
    ("6ade9755", ["Serenity Yoga", "yoga"], "generic list of place types, no name"),
    ("f8c5f88b", ["sports store", "downtown", "tennis racket"], "haven't mentioned where"),
    ("5d3d2817", ["startup", "previous", "former", "marketing specialist", "Televero"],
     "current job named, previous=not mentioned"),
    ("0a995998", ["pick up", "return", "clothing", "items"], "wrong count"),
    ("6d550036", ["project", "lead", "leading"], "wrong count"),
]


async def _haystack_text(user_id: str, *_unused) -> str:
    """Pull every message text for the user (we don't preserve LongMemEval session ids)."""
    db = get_database()
    cursor = db["chat_messages"].find(
        {"user_id": user_id},
        {"role": 1, "content": 1, "_id": 0},
    )
    out: list[str] = []
    async for m in cursor:
        out.append(f"{m['role']}: {m['content']}")
    return "\n".join(out)


async def _stored_state(user_id: str) -> tuple[str, str]:
    db = get_database()
    doc = await db["rolling_summary_state"].find_one({"user_id": user_id})
    if not doc:
        return "", ""
    return doc.get("user_profile", "") or "", doc.get("rolling_summary", "") or ""


def _grep(text: str, needle: str, max_lines: int = 4) -> list[str]:
    out: list[str] = []
    for ln in text.splitlines():
        if needle.lower() in ln.lower():
            out.append(ln.strip())
            if len(out) >= max_lines:
                break
    return out


async def main() -> int:
    target_qids = {qid for qid, _, _ in TARGETS}
    instance_by_qid = {}
    for inst in load_longmemeval(variant="s", limit=200):
        if inst.question_id in target_qids:
            instance_by_qid[inst.question_id] = inst
        if len(instance_by_qid) == len(target_qids):
            break

    for qid, anchors, our_answer in TARGETS:
        inst = instance_by_qid.get(qid)
        if inst is None:
            print(f"== {qid}: NOT FOUND IN DATASET ==")
            continue
        user_id = f"bench-{qid}"

        print(f"\n========== qid={qid}  qtype={inst.question_type} ==========")
        print(f"Q : {inst.question}")
        print(f"GT: {inst.answer!r}")
        print(f"OUR ANSWER: {our_answer}")
        print(f"answer_session_ids: {inst.answer_session_ids}")

        haystack = await _haystack_text(user_id, inst.answer_session_ids)
        if not haystack.strip():
            print("HAYSTACK: (no chat_messages found — user_id may have been cleaned)")
            continue

        # 1. Did the needle appear in the evidence sessions?
        print("\n# 1) Evidence-session transcript hits per anchor:")
        for a in anchors:
            hits = _grep(haystack, a, max_lines=3)
            if hits:
                print(f"  anchor={a!r}: {len(hits)} match(es)")
                for h in hits[:2]:
                    print(f"    > {h[:240]}")
            else:
                print(f"  anchor={a!r}: 0 matches")

        # 2. Did extraction preserve any anchor into stored state?
        profile, summary = await _stored_state(user_id)
        print("\n# 2) Stored state hits per anchor:")
        for a in anchors:
            ph = _grep(profile, a, max_lines=2)
            sh = _grep(summary, a, max_lines=2)
            print(f"  anchor={a!r}: profile_hits={len(ph)} summary_hits={len(sh)}")
            for h in ph[:1]:
                print(f"    P> {h[:240]}")
            for h in sh[:2]:
                print(f"    S> {h[:240]}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
