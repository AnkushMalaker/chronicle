#!/usr/bin/env python3
"""Drive a day-chain rebuild to a finished state without supervision.

``rebuild-memory --rebuild-from days`` queues one job per local day and steps over a
day whose write fails (``allow_failure=True``), which is right -- one bad day must not
stall thirty-eight good ones -- but it means the chain can finish with days still
incomplete and nothing scheduled to notice. This closes that gap: show the exact job
progress, wait for the chain to drain, re-enqueue every settled day that did not reach
``written`` or ``no_changes``, and only then validate the rebuilt vault.

    python src/scripts/finish_day_rebuild.py --user-id <id> [--max-retry-rounds 3]

Safe to re-run: re-enqueueing a written day is a no-op because the ``memory_state``
latch skips it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from advanced_omi_backend.controllers.queue_controller import memory_queue
from advanced_omi_backend.database import get_database
from advanced_omi_backend.services.memory.rebuild import (
    JOB_RESULT_TTL,
    TIMELINE_REBUILD_JOB_TIMEOUT,
    build_timeline_days,
)
from advanced_omi_backend.services.memory.vault_scaffold import canonical_vault_scaffold
from advanced_omi_backend.services.memory.vault_verify import verify_vault_changes
from advanced_omi_backend.workers.timeline_jobs import rebuild_timeline_day_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("finish_day_rebuild")

POLL_SECONDS = 60
VAULT_ROOT = Path("/app/data/conversation_docs")
COMPLETE_MEMORY_STATES = frozenset({"written", "no_changes"})
TERMINAL_JOB_STATES = frozenset({"finished", "failed", "stopped", "canceled"})
REQUIRED_SCAFFOLD_PATHS = (
    "People.md",
    "Conversations.md",
    "Topics.md",
    "Templates/Person Template.md",
    "Templates/Conversation Template.md",
    "Templates/Topic Template.md",
    "Templates/Bases/People.base",
    "Templates/Bases/Conversations.base",
    "Templates/Bases/Topics.base",
)
console = Console()


class RebuildProgressDisplay:
    """Three durable rows for the asynchronous portion of a day rebuild."""

    def __init__(self, total_jobs: int, total_settled_days: int):
        self.progress = Progress(
            SpinnerColumn(finished_text="[green]✓[/green]"),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[dim]{task.fields[detail]}[/dim]"),
            TimeElapsedColumn(),
            console=console,
        )
        self.chain = self.progress.add_task(
            "Stage 1/3 · Process day chain",
            total=max(total_jobs, 1),
            detail="Waiting for workers",
        )
        self.repair = self.progress.add_task(
            "Stage 2/3 · Repair incomplete settled days",
            total=max(total_settled_days, 1),
            detail="Waiting for day chain",
            start=False,
        )
        self.validate = self.progress.add_task(
            "Stage 3/3 · Validate rebuilt vault",
            total=1,
            detail="Waiting for repairs",
            start=False,
        )

    def __enter__(self):
        self.progress.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.progress.__exit__(exc_type, exc_value, traceback)

    def update_chain(self, completed: int, total: int, states: Counter) -> None:
        active = states["started"] + states["queued"]
        self.progress.update(
            self.chain,
            completed=min(completed, max(total, 1)),
            total=max(total, 1),
            detail=(
                f"{completed}/{total} jobs terminal; {active} active, "
                f"{states['deferred']} deferred"
            ),
        )

    def complete_chain(self, total: int) -> None:
        self.progress.update(
            self.chain,
            completed=max(total, 1),
            total=max(total, 1),
            detail=f"{total}/{total} jobs processed",
        )
        self.progress.stop_task(self.chain)

    def update_repairs(
        self, complete: int, total: int, *, detail: str, finished: bool = False
    ) -> None:
        if not self.progress.tasks[self.repair].started:
            self.progress.start_task(self.repair)
        self.progress.update(
            self.repair,
            completed=max(total, 1) if finished else min(complete, max(total, 1)),
            total=max(total, 1),
            detail=detail,
        )
        if finished:
            self.progress.stop_task(self.repair)

    def complete_validation(self, detail: str) -> None:
        self.progress.start_task(self.validate)
        self.progress.update(self.validate, completed=1, detail=detail)
        self.progress.stop_task(self.validate)


async def _chain_idle() -> bool:
    """True once no day job is queued, deferred, or executing."""
    connection = memory_queue.connection
    if memory_queue.count or memory_queue.deferred_job_registry.count:
        return False
    return not connection.keys("rq:executions:timeline_*")


def _rebuild_job_states(run_id: str) -> Counter:
    """Count the exact jobs from one rebuild, including deferred jobs."""
    states: Counter = Counter()
    connection = memory_queue.connection
    pattern = f"rq:job:timeline_rebuild_{run_id}_*"
    for key in connection.scan_iter(match=pattern):
        key_type = connection.type(key)
        if key_type not in ("hash", b"hash"):
            continue
        raw = connection.hget(key, "status")
        status = raw.decode() if isinstance(raw, bytes) else str(raw or "unknown")
        states[status] += 1
    return states


async def _expected_settled_days(database, user_id: str) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    return [
        {
            "local_date": day.local_date,
            "timezone": day.timezone,
        }
        for day in await build_timeline_days(database, (user_id,))
        if day.local_date < today
    ]


async def _unwritten_days(
    database, user_id: str, expected_days: list[dict]
) -> list[dict]:
    """Days that never reached ``written``.

    Today is excluded deliberately: the day pass only records *settled* days, so the
    current local date legitimately carries no memory fields and re-queueing it would
    loop forever.
    """
    stored = {}
    cursor = database["timeline_days"].find(
        {"user_id": user_id},
        projection={
            "local_date": 1,
            "timezone": 1,
            "memory_state": 1,
            "memory_attempts": 1,
            "memory_error": 1,
        },
    )
    async for document in cursor:
        local_date = document.get("local_date")
        if isinstance(local_date, datetime):
            local_date = local_date.date()
        if isinstance(local_date, date):
            stored[(local_date, document.get("timezone") or "UTC")] = document

    # ``build_timeline_days`` deliberately starts from durable raw capture. A day can
    # therefore be in the rebuild plan even when all of its chunks are silence,
    # quarantined, or otherwise ineligible as Timeline evidence. The analysis entry
    # point records that honest outcome as a completed ``awaiting_evidence`` run and
    # returns ``memory=no_day``. Treat the latest such run as terminal: retrying cannot
    # manufacture evidence and would make an otherwise healthy rebuild fail its repair
    # budget. Inspect the latest run of *every* state so an older empty result cannot
    # mask a newer pending or failed attempt.
    latest_runs: dict[tuple[date, str], dict] = {}
    cursor = database["timeline_analysis_runs"].find(
        {"user_id": user_id},
        projection={
            "local_date": 1,
            "timezone": 1,
            "state": 1,
            "created_at": 1,
            "completed_at": 1,
        },
    )
    async for document in cursor:
        local_date = document.get("local_date")
        if isinstance(local_date, datetime):
            local_date = local_date.date()
        if not isinstance(local_date, date):
            continue
        key = (local_date, document.get("timezone") or "UTC")
        previous = latest_runs.get(key)
        if previous is None or document.get("created_at", datetime.min) > previous.get(
            "created_at", datetime.min
        ):
            latest_runs[key] = document
    completed_empty_days = {
        key
        for key, run in latest_runs.items()
        if run.get("state") == "awaiting_evidence"
        and run.get("completed_at") is not None
    }

    rows = []
    for expected in expected_days:
        key = (expected["local_date"], expected["timezone"])
        document = stored.get(key)
        if document and document.get("memory_state") in COMPLETE_MEMORY_STATES:
            continue
        if key in completed_empty_days:
            continue
        rows.append(
            {
                **expected,
                "state": document.get("memory_state") if document else "missing",
                "attempts": document.get("memory_attempts") if document else 0,
                "error": document.get("memory_error") if document else None,
            }
        )
    return sorted(rows, key=lambda row: row["local_date"])


async def _settled_completion(database, user_id: str, expected_days: list[dict]) -> int:
    return len(expected_days) - len(
        await _unwritten_days(database, user_id, expected_days)
    )


def _enqueue(
    user_id: str,
    row: dict,
    *,
    repair_scope: str,
    sequence: str,
    depends_on,
):
    return memory_queue.enqueue(
        rebuild_timeline_day_job,
        user_id,
        row["local_date"].isoformat(),
        row["timezone"],
        job_timeout=TIMELINE_REBUILD_JOB_TIMEOUT,
        result_ttl=JOB_RESULT_TTL,
        job_id=(f"day_retry_{repair_scope}_{sequence}_{row['local_date'].isoformat()}"),
        description=f"Retry day {row['local_date'].isoformat()}",
        depends_on=depends_on,
        meta={
            "user_id": user_id,
            "rebuild_run_id": repair_scope,
            "local_date": row["local_date"].isoformat(),
            "trigger": "timeline_rebuild_repair",
        },
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


def _require_complete(remaining: list[dict]) -> None:
    """Never let the unattended wrapper report a partial rebuild as healthy."""
    if not remaining:
        return
    dates = ", ".join(row["local_date"].isoformat() for row in remaining)
    raise RuntimeError(
        f"day rebuild incomplete after repair budget: {len(remaining)} day(s): {dates}"
    )


def _require_valid_vault(root: Path) -> int:
    """Validate the complete rebuilt vault as if every note were newly created."""

    canonical_scaffold = canonical_vault_scaffold()
    missing_scaffold = [
        relative
        for relative in REQUIRED_SCAFFOLD_PATHS
        if not (root / relative).is_file()
    ]
    if missing_scaffold:
        raise RuntimeError(
            "rebuilt vault is missing required scaffold files: "
            + ", ".join(missing_scaffold)
        )
    changed_scaffold = [
        relative
        for relative in REQUIRED_SCAFFOLD_PATHS
        if (root / relative).read_text(encoding="utf-8") != canonical_scaffold[relative]
    ]
    if changed_scaffold:
        raise RuntimeError(
            "rebuilt vault changed canonical scaffold files: "
            + ", ".join(changed_scaffold)
        )
    findings = verify_vault_changes(root, canonical_scaffold)
    if findings:
        preview = "; ".join(
            f"{finding.path} [{finding.rule}]" for finding in findings[:20]
        )
        suffix = f"; and {len(findings) - 20} more" if len(findings) > 20 else ""
        raise RuntimeError(
            f"rebuilt vault failed structural validation: {len(findings)} "
            f"finding(s): {preview}{suffix}"
        )
    return len(list(root.rglob("*.md"))) if root.is_dir() else 0


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--run-id",
        help="Rebuild run ID used to show exact queued/active/completed job progress",
    )
    parser.add_argument("--max-retry-rounds", type=int, default=3)
    parser.add_argument("--poll-seconds", type=int, default=POLL_SECONDS)
    args = parser.parse_args()
    repair_scope = args.run_id or uuid.uuid4().hex[:12]

    database = get_database()
    await database.command("ping")
    expected_days = await _expected_settled_days(database, args.user_id)
    initial_states = _rebuild_job_states(args.run_id) if args.run_id else Counter()
    total_jobs = sum(initial_states.values()) or len(expected_days)

    with RebuildProgressDisplay(total_jobs, len(expected_days)) as progress:
        while not await _chain_idle():
            if args.run_id:
                states = _rebuild_job_states(args.run_id)
                observed_total = sum(states.values()) or total_jobs
                terminal = sum(states[state] for state in TERMINAL_JOB_STATES)
                progress.update_chain(terminal, observed_total, states)
            else:
                complete = await _settled_completion(
                    database, args.user_id, expected_days
                )
                progress.update_chain(complete, total_jobs, Counter())
            await asyncio.sleep(args.poll_seconds)
        progress.complete_chain(total_jobs)
        log.info("day chain drained")

        for attempt in range(1, args.max_retry_rounds + 1):
            pending = await _unwritten_days(database, args.user_id, expected_days)
            complete = len(expected_days) - len(pending)
            progress.update_repairs(
                complete,
                len(expected_days),
                detail=f"{complete}/{len(expected_days)} settled days complete",
            )
            if not pending:
                log.info("every settled day is complete")
                progress.update_repairs(
                    len(expected_days),
                    len(expected_days),
                    detail=f"{len(expected_days)} settled days complete",
                    finished=True,
                )
                break
            log.info(
                "retry round %d: %d incomplete day(s): %s",
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
                    repair_scope=repair_scope,
                    sequence=f"{attempt}_{sequence}",
                    depends_on=dependency,
                )
                dependency = job.id
            await asyncio.sleep(args.poll_seconds)
            while not await _chain_idle():
                complete = await _settled_completion(
                    database, args.user_id, expected_days
                )
                progress.update_repairs(
                    complete,
                    len(expected_days),
                    detail=(
                        f"Retry {attempt}/{args.max_retry_rounds}; "
                        f"{complete}/{len(expected_days)} settled days complete"
                    ),
                )
                await asyncio.sleep(args.poll_seconds)
        else:
            remaining = await _unwritten_days(database, args.user_id, expected_days)
            if remaining:
                log.warning(
                    "gave up with %d incomplete day(s): %s",
                    len(remaining),
                    ", ".join(row["local_date"].isoformat() for row in remaining),
                )
            progress.update_repairs(
                len(expected_days) - len(remaining),
                len(expected_days),
                detail=f"{len(remaining)} incomplete after retry budget",
                finished=True,
            )

        removed = _strip_empty_notes(args.user_id)
        log.info(
            "removed %d empty note(s): %s",
            len(removed),
            ", ".join(removed) or "none",
        )
        remaining = await _unwritten_days(database, args.user_id, expected_days)
        _require_complete(remaining)
        notes = _require_valid_vault(VAULT_ROOT / args.user_id)

        states = {
            state: await database["timeline_days"].count_documents(
                {"user_id": args.user_id, "memory_state": state}
            )
            for state in ("written", "no_changes", "partial", "skipped")
        }
        total = await database["timeline_days"].count_documents(
            {"user_id": args.user_id}
        )
        progress.complete_validation(
            f"{states['written']} written, {states['no_changes']} no-change, "
            f"{states['partial']} partial, {states['skipped']} skipped; {notes} notes"
        )
        log.info(
            "FINAL written=%d no_changes=%d partial=%d skipped=%d/%d timeline_days, "
            "vault notes=%d",
            states["written"],
            states["no_changes"],
            states["partial"],
            states["skipped"],
            total,
            notes,
        )


if __name__ == "__main__":
    asyncio.run(main())
