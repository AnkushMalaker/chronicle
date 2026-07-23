"""Pull the LongMemEval question + Chronicle's stored state for one failing qid.

Usage (inside chronicle-backend container):
    python -m benchmark._inspect_failure 58bf7951
"""

from __future__ import annotations

import asyncio
import sys

from advanced_omi_backend.database import get_database
from advanced_omi_backend.services.memory.providers.rolling_summary import (
    COLLECTION_NAME,
)

from benchmark.loader import load_longmemeval


async def main(qid: str) -> int:
    # Find the instance in the dataset
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

    print("# Question")
    print(instance.question)
    print()
    print("# Ground truth answer")
    print(instance.answer)
    print()
    print(f"# Sessions: {len(instance.sessions)}")
    print(f"# Answer session ids: {instance.answer_session_ids}")
    print()

    # Pull the rolling_summary doc
    db = get_database()
    doc = await db[COLLECTION_NAME].find_one({"user_id": f"bench-{qid}"})
    if doc is None:
        print("(no rolling_summary doc — was cleaned up)")
        return 0

    print(
        f"# Stored state — fact_count={doc.get('fact_count')} "
        f"profile_chars={len(doc.get('user_profile') or '')} "
        f"summary_chars={len(doc.get('rolling_summary') or '')} "
        f"tokens_est={doc.get('summary_tokens_est')}"
    )
    print()
    print("## User profile")
    print(doc.get("user_profile") or "(empty)")
    print()
    print("## Rolling summary")
    print(doc.get("rolling_summary") or "(empty)")
    print()

    # Search for evidence-session keywords inside the summary
    return 0


if __name__ == "__main__":
    qid = sys.argv[1] if len(sys.argv) > 1 else "58bf7951"
    sys.exit(asyncio.run(main(qid)))
