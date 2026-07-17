"""Read-only scope: find conversations whose ACTIVE transcript version has words/text
but an empty segments list (so the WebUI renders blank). Breaks them down by
diarization_source to distinguish pyannote-wiped from provider/never-diarized.

Run inside the backend/worker container (read-only, no writes):
    python3 /app/scope_blank_segments.py
"""

import asyncio
import sys
from collections import Counter

from beanie import init_beanie

from advanced_omi_backend.database import db
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User


async def main() -> None:
    await init_beanie(database=db, document_models=[User, Conversation])

    by_diar = Counter()
    rows = []
    async for c in Conversation.find_all():
        if c.deleted:
            continue
        av = c.active_transcript
        if not av:
            continue
        has_text = bool((av.transcript or "").strip())
        words = av.words or []
        segs = av.segments or []
        if (has_text or words) and not segs:
            diar = av.diarization_source or "(none)"
            by_diar[diar] += 1
            word_spk = sorted({w.speaker for w in words if w.speaker is not None})
            rows.append(
                (
                    c.conversation_id,
                    c.processing_status,
                    c.client_id,
                    round(c.audio_total_duration or 0, 1),
                    len(words),
                    word_spk,
                    diar,
                    av.provider,
                    (c.title or "")[:40],
                )
            )

    print(
        f"=== {len(rows)} non-deleted convs: active version has words/text but 0 segments ==="
    )
    print(f"by diarization_source: {dict(by_diar)}\n")
    rows.sort(key=lambda r: -r[3])
    print(
        f"{'conv':10} {'status':10} {'client':20} {'dur':>7} {'wrds':>4} {'wordspk':12} {'diar':10} {'provider':10} title"
    )
    for cid, st, client, dur, nw, wspk, diar, prov, title in rows:
        print(
            f"{cid[:8]:10} {str(st):10} {str(client):20} {dur:>7} {nw:>4} {str(wspk):12} {str(diar):10} {str(prov):10} {title!r}"
        )


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
