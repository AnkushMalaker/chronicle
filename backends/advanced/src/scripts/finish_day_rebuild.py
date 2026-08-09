#!/usr/bin/env python3
"""Drive a day-chain rebuild to a finished state without supervision.

``rebuild-memory --rebuild-from days`` queues one job per local day and steps over a
day whose write fails (``allow_failure=True``), which is right -- one bad day must not
stall thirty-eight good ones -- but it means the chain can finish with days still
unwritten and nothing scheduled to notice. This closes that gap: wait for the chain to
drain, re-enqueue every day that did not reach ``written``, and only then run the
post-chain steps that assume a complete timeline.

    python src/scripts/finish_day_rebuild.py --user-id <id> [--max-retry-rounds 3]

Safe to re-run: re-enqueueing a written day is a no-op because the ``memory_state``
latch skips it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from advanced_omi_backend.controllers.queue_controller import memory_queue
from advanced_omi_backend.database import get_database
from advanced_omi_backend.services.memory.rebuild import (
    JOB_RESULT_TTL,
    TIMELINE_REBUILD_JOB_TIMEOUT,
)
from advanced_omi_backend.workers.timeline_jobs import rebuild_timeline_day_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("finish_day_rebuild")

POLL_SECONDS = 60
VAULT_ROOT = Path("/app/data/conversation_docs")


async def _chain_idle() -> bool:
    """True once no day job is queued, deferred, or executing."""
    connection = memory_queue.connection
    if memory_queue.count or memory_queue.deferred_job_registry.count:
        return False
    return not connection.keys("rq:executions:timeline_*")


async def _unwritten_days(database, user_id: str) -> list[dict]:
    """Days that never reached ``written``.

    Today is excluded deliberately: the day pass only records *settled* days, so the
    current local date legitimately carries no memory fields and re-queueing it would
    loop forever.
    """
    today = datetime.now(timezone.utc).date()
    rows = []
    cursor = database["timeline_days"].find({"user_id": user_id})
    async for document in cursor:
        if document.get("memory_state") == "written":
            continue
        local_date = document.get("local_date")
        if isinstance(local_date, datetime):
            local_date = local_date.date()
        if not isinstance(local_date, date) or local_date >= today:
            continue
        rows.append(
            {
                "local_date": local_date,
                "timezone": document.get("timezone") or "UTC",
                "state": document.get("memory_state"),
                "attempts": document.get("memory_attempts"),
                "error": document.get("memory_error"),
            }
        )
    return sorted(rows, key=lambda row: row["local_date"])


def _enqueue(user_id: str, row: dict, *, sequence: str, depends_on):
    return memory_queue.enqueue(
        rebuild_timeline_day_job,
        user_id,
        row["local_date"].isoformat(),
        row["timezone"],
        job_timeout=TIMELINE_REBUILD_JOB_TIMEOUT,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"day_retry_{sequence}_{row['local_date'].isoformat()}",
        description=f"Retry day {row['local_date'].isoformat()}",
        depends_on=depends_on,
    )


def _strip_empty_notes(user_id: str) -> list[str]:
    """Delete zero-byte notes the write agent created and abandoned.

    A day write anchors on ``Daily/<local_date>.md`` and must never mint a
    ``Conversations/`` note; both show up empty when the agent opens a path and never
    fills it. Only zero-byte files are touched, so nothing with content can be lost.
    """
    root = VAULT_ROOT / user_id
    removed = []
    if not root.is_dir():
        return removed
    for path in sorted(root.rglob("*.md")):
        if path.stat().st_size == 0:
            path.unlink()
            removed.append(str(path.relative_to(root)))
    return removed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--max-retry-rounds", type=int, default=3)
    args = parser.parse_args()

    database = get_database()
    await database.command("ping")

    while not await _chain_idle():
        await asyncio.sleep(POLL_SECONDS)
    log.info("day chain drained")

    for attempt in range(1, args.max_retry_rounds + 1):
        pending = await _unwritten_days(database, args.user_id)
        if not pending:
            log.info("every settled day is written")
            break
        log.info(
            "retry round %d: %d unwritten day(s): %s",
            attempt,
            len(pending),
            ", ".join(row["local_date"].isoformat() for row in pending),
        )
        # Serial, same as the original chain: each write takes this user's vault lock.
        dependency = None
        for sequence, row in enumerate(pending, start=1):
            job = _enqueue(
                args.user_id,
                row,
                sequence=f"{attempt}_{sequence}",
                depends_on=dependency,
            )
            dependency = job.id
        await asyncio.sleep(POLL_SECONDS)
        while not await _chain_idle():
            await asyncio.sleep(POLL_SECONDS)
    else:
        remaining = await _unwritten_days(database, args.user_id)
        if remaining:
            log.warning(
                "gave up with %d day(s) unwritten: %s",
                len(remaining),
                ", ".join(row["local_date"].isoformat() for row in remaining),
            )

    removed = _strip_empty_notes(args.user_id)
    log.info("removed %d empty note(s): %s", len(removed), ", ".join(removed) or "none")

    written = await database["timeline_days"].count_documents(
        {"user_id": args.user_id, "memory_state": "written"}
    )
    total = await database["timeline_days"].count_documents({"user_id": args.user_id})
    notes = len(list((VAULT_ROOT / args.user_id).rglob("*.md")))
    log.info("FINAL written=%d/%d timeline_days, vault notes=%d", written, total, notes)


if __name__ == "__main__":
    asyncio.run(main())
