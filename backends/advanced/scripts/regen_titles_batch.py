"""Batch-regenerate titles/summaries for completed conversations stuck with a
placeholder title (e.g. "Recording...", "Audio Recording (Transcription Failed)").

These conversations have a real transcript but never got an LLM title (completed via
a path that skipped generate_title_summary, or the title job failed). This enqueues
generate_title_summary_job (reads the existing transcript — NO re-transcription) in
batches of BATCH_SIZE, polling until each title changes off the placeholder.

Run inside the backend/worker container:
    python3 /app/regen_titles_batch.py            # dry run (lists targets)
    python3 /app/regen_titles_batch.py --apply    # enqueue + poll
"""

import argparse
import asyncio
import sys
import time

from beanie import init_beanie

from advanced_omi_backend.database import db
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User

BATCH_SIZE = 5
POLL_SECS = 20
MAX_WAIT_SECS = 8 * 60

PLACEHOLDER_TITLES = ("Reprocessing...", "Recording...", "Transcribing...")


def _is_placeholder_title(title: str) -> bool:
    return "Audio Recording (" in title or title in PLACEHOLDER_TITLES


def _is_placeholder_conv(c: "Conversation") -> bool:
    t = c.title or ""
    return bool(t) and _is_placeholder_title(t)


async def _targets() -> list:
    out = []
    async for c in Conversation.find_all():
        if (
            not c.deleted
            and c.processing_status == Conversation.ConversationStatus.COMPLETED.value
            and _is_placeholder_conv(c)
        ):
            out.append(c.conversation_id)
    return out


async def _title_of(cid: str):
    c = await Conversation.find_one(Conversation.conversation_id == cid)
    return (c.title or "") if c else ""


async def main(apply: bool) -> None:
    await init_beanie(database=db, document_models=[User, Conversation])
    ids = await _targets()
    print(f"=== {len(ids)} completed convs with placeholder titles ===", flush=True)
    for cid in ids:
        print(f"  {cid[:12]} title={await _title_of(cid)!r}", flush=True)
    if not apply:
        print("Dry run — re-run with --apply to enqueue.", flush=True)
        return

    # Import the queue lazily (module-level side effects: redis/queue setup).
    from advanced_omi_backend.controllers.queue_controller import default_queue
    from advanced_omi_backend.workers.conversation_jobs import (
        generate_title_summary_job,
    )

    done = {}
    nbatches = (len(ids) + BATCH_SIZE - 1) // BATCH_SIZE
    for b in range(nbatches):
        batch = ids[b * BATCH_SIZE : (b + 1) * BATCH_SIZE]
        print(f"--- BATCH {b+1}/{nbatches}: {[c[:8] for c in batch]} ---", flush=True)
        for cid in batch:
            default_queue.enqueue(
                generate_title_summary_job,
                cid,
                job_id=f"regen_title_{cid[:12]}",
                job_timeout=300,
                description=f"Regenerate title/summary for {cid[:8]}",
            )
            print(f"  enqueued {cid[:8]}", flush=True)

        deadline = time.time() + MAX_WAIT_SECS
        while True:
            await asyncio.sleep(POLL_SECS)
            pending = []
            for cid in batch:
                t = await _title_of(cid)
                if (not t) or _is_placeholder_title(t):
                    pending.append(cid)
                else:
                    done[cid] = t
            print(
                f"  poll: {len(batch)-len(pending)}/{len(batch)} retitled; "
                f"pending={[c[:8] for c in pending]}",
                flush=True,
            )
            if not pending or time.time() > deadline:
                if pending:
                    print(
                        f"  batch {b+1}: TIMEOUT, pending={[c[:8] for c in pending]}",
                        flush=True,
                    )
                break

    print("=== FINAL ===", flush=True)
    for cid in ids:
        print(f"  {cid[:12]} -> {done.get(cid, '(unchanged)')!r}", flush=True)
    print(f"Retitled {len(done)}/{len(ids)}.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
    sys.exit(0)
