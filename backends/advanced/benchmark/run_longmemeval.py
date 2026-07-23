"""LongMemEval benchmark runner.

Per-instance lifecycle, written to ``runs/<run_id>/progress.jsonl`` after
each transition (fsynced):

    cleanup_user             → status: ingesting
    ingest each session      → status: ingested  (memory_count summed)
    retrieve + answer        → status: answered  (answer + extraction_model)
    judge                    → status: judged    (score + judge_model)
    final write              → status: done
    cleanup_user (opt-in)    → instance's data wiped from Mongo + memory + KG

Each instance gets its own per-user FalkorDB graph (``chronicle_bench-<qid>``)
via the chronicle MemoryService + KnowledgeGraphService refactor. That makes
cross-instance retrieval contamination impossible at the storage layer, so
**by default we keep the data after each instance** — the operator can then
inspect what was extracted with e.g.::

    redis-cli -p 6381 GRAPH.QUERY chronicle_bench-<qid> \
        "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n"

Pass ``--cleanup-after-done`` if you want the runner to drop each per-user
graph after the instance reaches ``done`` (smaller disk footprint at the
cost of post-run inspection).

Resume: re-running with ``--run-id <id>`` reads progress.jsonl, skips
``done`` instances, and restarts everything else from cleanup. Per-user
state is cheap to recreate, so we prefer redo over partial recovery.

While the run is in flight the runner emits a one-line tally per finished
instance — cumulative accuracy and per-question-type counts — so progress
is visible without grep-ing through the full log.

Examples
--------
    # Smoke run on one instance — data is kept for later inspection
    python -m benchmark.run_longmemeval --variant s --limit 1

    # Resume a previous run
    python -m benchmark.run_longmemeval --run-id 20260128-181200-a3f5e2

    # Drop per-user graphs after each instance to bound disk/memory
    python -m benchmark.run_longmemeval --variant s --limit 50 --cleanup-after-done
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from advanced_omi_backend.llm_client import get_llm_client
from advanced_omi_backend.model_registry import get_models_registry

from .ingest import cleanup_user, ingest_chat_session
from .judge import judge_answer, resolve_judge_model
from .loader import LongMemEvalInstance, load_longmemeval
from .progress import JudgeCache, ProgressEntry, ProgressFile, open_run
from .retrieve import build_answer_prompt, retrieve_context

logger = logging.getLogger(__name__)


def _resolve_extraction_model() -> str:
    registry = get_models_registry()
    if registry is not None:
        default = registry.get_default("llm")
        if default and default.model_name:
            return default.model_name
    return get_llm_client().get_default_model()


def _user_id_for(instance: LongMemEvalInstance) -> str:
    """Per-instance namespace; cleanup is cheap, so collisions are not a concern."""
    return f"bench-{instance.question_id}"


async def _process_instance(
    instance: LongMemEvalInstance,
    progress: ProgressFile,
    judge_cache: JudgeCache,
    extraction_model: str,
    cleanup_after_done: bool,
) -> ProgressEntry:
    user_id = _user_id_for(instance)
    qid = instance.question_id

    t_start = time.perf_counter()

    progress.append(
        ProgressEntry(
            question_id=qid,
            status="ingesting",
            user_id=user_id,
            question_type=instance.question_type,
            extraction_model=extraction_model,
        )
    )
    t0 = time.perf_counter()
    await cleanup_user(user_id)
    t_cleanup = time.perf_counter() - t0

    total_memories = 0
    sorted_sessions = sorted(instance.sessions, key=lambda s: s.date)
    t0 = time.perf_counter()
    for session in sorted_sessions:
        if not session.turns:
            continue
        _, count, success = await ingest_chat_session(
            user_id=user_id,
            turns=session.turns,
            session_date=session.date,
        )
        total_memories += count
        if not success:
            logger.warning("ingest failed for qid=%s session=%s", qid, session.session_id)
    t_ingest = time.perf_counter() - t0

    progress.update(qid, status="ingested")
    logger.info(
        "qid=%s: ingested %d sessions, %d memories",
        qid,
        len(sorted_sessions),
        total_memories,
    )

    qdate = instance.question_date.isoformat() if instance.question_date else None
    t0 = time.perf_counter()
    context = await retrieve_context(query=instance.question, user_id=user_id)
    t_retrieve = time.perf_counter() - t0
    prompt = build_answer_prompt(
        question=instance.question, context=context, question_date=qdate
    )
    client = get_llm_client()
    t0 = time.perf_counter()
    answer = client.generate(prompt=prompt).strip()
    t_answer = time.perf_counter() - t0

    progress.update(qid, status="answered", answer=answer)

    # Judge runs sync (paper does too); cache-checked first so re-runs are free.
    label, _raw, judge_model = judge_answer(
        question_id=qid,
        question_type=instance.question_type,
        question=instance.question,
        ground_truth=instance.answer,
        answer=answer,
        abstention=instance.is_abstention,
        cache=judge_cache,
    )
    t0 = time.perf_counter()
    progress.update(
        qid, status="judged", score=label, judge_model=judge_model
    )
    t_judge = time.perf_counter() - t0
    final = progress.update(qid, status="done")

    t_total = time.perf_counter() - t_start
    logger.info(
        "qid=%s: PROFILE total=%.1fs cleanup=%.1fs ingest=%.1fs (n=%d) "
        "retrieve=%.2fs answer=%.2fs judge=%.2fs",
        qid,
        t_total,
        t_cleanup,
        t_ingest,
        len(sorted_sessions),
        t_retrieve,
        t_answer,
        t_judge,
    )

    if cleanup_after_done:
        # fsync of the done row already happened above — wipe is best-effort
        # from here. A crash mid-cleanup just leaves stale rows for the next
        # ``--run-id`` resume to clean up at instance start.
        await cleanup_user(user_id)
        logger.info("qid=%s: cleaned up post-done", qid)
    return final


class _RunningTally:
    """Cumulative judged-correct counts, printed one line per finished instance.

    Counts only ``done`` entries with non-null scores. Resumed instances are
    folded in too, marked ``(resumed)`` so the operator can tell them apart
    from work performed in the current process.
    """

    def __init__(self) -> None:
        self.judged = 0
        self.correct = 0
        self.by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [correct, judged]

    def record(self, entry: ProgressEntry, *, resumed: bool) -> None:
        if entry.status != "done" or entry.score is None:
            return
        self.judged += 1
        score_int = 1 if entry.score else 0
        self.correct += score_int
        if entry.question_type:
            slot = self.by_type[entry.question_type]
            slot[0] += score_int
            slot[1] += 1
        suffix = " (resumed)" if resumed else ""
        running_acc = self.correct / self.judged if self.judged else 0.0
        by_type_str = "  ".join(
            f"{qt}={c}/{n}"
            for qt, (c, n) in sorted(self.by_type.items())
        )
        print(
            f"qid={entry.question_id}  qtype={entry.question_type}  "
            f"score={entry.score}  running: {self.correct}/{self.judged} "
            f"({running_acc:.4f})  by_type: {by_type_str}{suffix}",
            flush=True,
        )


async def _run(
    *,
    variant: str,
    limit: Optional[int],
    run_id: Optional[str],
    runs_root: Path,
    cleanup_after_done: bool,
    question_type: Optional[str] = None,
    workers: int = 1,
) -> tuple[str, list[ProgressEntry]]:
    rid, run_dir, progress, cache = open_run(run_id, runs_root)
    if run_id is None:
        # Always print this on the first line so the operator can grab it
        # even if the rest of the output gets truncated.
        print(f"# run_id={rid}  run_dir={run_dir}", flush=True)
    else:
        print(f"# resuming run_id={rid}  run_dir={run_dir}", flush=True)

    # Surface the resolved judge model up-front; record per-row in progress.
    extraction_model = _resolve_extraction_model()
    logger.info(
        "extraction_model=%s  judge_model=%s",
        extraction_model,
        resolve_judge_model(),
    )

    tally = _RunningTally()
    completed: list[ProgressEntry] = []
    # When filtering by question_type we can't pass `limit` to the loader —
    # the loader applies it BEFORE the filter and would short-circuit after
    # N rows of the wrong type. Stream everything and apply the cap below.
    raw_iter = load_longmemeval(variant=variant, limit=None if question_type else limit)
    kept = 0
    instances = []
    for inst in raw_iter:
        if question_type and inst.question_type != question_type:
            continue
        instances.append(inst)
        kept += 1
        if limit is not None and kept >= limit:
            break
    if question_type:
        logger.info("question_type filter %r kept %d instance(s)", question_type, len(instances))

    pending: list[LongMemEvalInstance] = []
    for instance in instances:
        if progress.is_done(instance.question_id):
            entry = progress.get(instance.question_id)
            assert entry is not None
            completed.append(entry)
            tally.record(entry, resumed=True)
            continue
        pending.append(instance)

    if workers < 1:
        workers = 1
    logger.info("dispatching %d instance(s) with workers=%d", len(pending), workers)

    sem = asyncio.Semaphore(workers)
    tally_lock = asyncio.Lock()

    async def _run_one(instance: LongMemEvalInstance) -> None:
        async with sem:
            try:
                entry = await _process_instance(
                    instance=instance,
                    progress=progress,
                    judge_cache=cache,
                    extraction_model=extraction_model,
                    cleanup_after_done=cleanup_after_done,
                )
                async with tally_lock:
                    completed.append(entry)
                    tally.record(entry, resumed=False)
            except Exception as exc:  # noqa: BLE001 — the runner must not die on one bad instance
                logger.exception("qid=%s failed: %s", instance.question_id, exc)
                err = traceback.format_exc(limit=2)
                progress.append(
                    ProgressEntry(
                        question_id=instance.question_id,
                        status="error",
                        user_id=_user_id_for(instance),
                        question_type=instance.question_type,
                        extraction_model=extraction_model,
                        error=err,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                )

    await asyncio.gather(*(_run_one(inst) for inst in pending))

    return rid, completed


def _aggregate(entries: list[ProgressEntry]) -> dict:
    by_type: dict[str, list[bool]] = defaultdict(list)
    judged: list[bool] = []
    for e in entries:
        if e.status != "done" or e.score is None:
            continue
        judged.append(e.score)
        if e.question_type:
            by_type[e.question_type].append(e.score)
    return {
        "n": len(judged),
        "accuracy": (sum(judged) / len(judged)) if judged else 0.0,
        "by_question_type": {
            qt: {"n": len(scores), "accuracy": sum(scores) / len(scores)}
            for qt, scores in by_type.items()
        },
    }


def _print_summary(rid: str, summary: dict) -> None:
    print(f"\n=== Run {rid} ===")
    print(f"  judged: {summary['n']}  accuracy: {summary['accuracy']:.4f}")
    if summary["by_question_type"]:
        print("  by question_type:")
        for qt in sorted(summary["by_question_type"]):
            row = summary["by_question_type"][qt]
            print(f"    {qt:30s} n={row['n']:>3}  acc={row['accuracy']:.4f}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LongMemEval runner for Chronicle")
    parser.add_argument("--variant", choices=("s", "m", "oracle"), default="s")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap on number of instances (smoke flag)")
    parser.add_argument("--run-id", default=None,
                        help="Resume a previous run; omit to start a new one")
    parser.add_argument("--runs-dir", default="runs",
                        help="Where to put runs/<run_id>/ (default: ./runs)")
    parser.add_argument(
        "--cleanup-after-done",
        action="store_true",
        help=(
            "Drop the per-user FalkorDB graph for an instance after it reaches "
            "done. Off by default — per-user graphs naturally isolate retrieval "
            "and we keep them so the operator can inspect what each instance "
            "extracted. Turn on for long runs where disk/memory matter."
        ),
    )
    parser.add_argument(
        "--question-type",
        default=None,
        help=(
            "Restrict the run to a single LongMemEval question type "
            "(e.g. multi-session, knowledge-update, temporal-reasoning, "
            "single-session-preference, single-session-assistant, single-session-user). "
            "Default: all types."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of instances to process concurrently. Each instance has its "
            "own bench-<qid> user_id so storage is non-overlapping; the bottleneck "
            "is OpenAI rate limits. 5–10 is a reasonable default for gpt-4o-mini."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    rid, entries = asyncio.run(
        _run(
            variant=args.variant,
            limit=args.limit,
            run_id=args.run_id,
            runs_root=Path(args.runs_dir),
            cleanup_after_done=args.cleanup_after_done,
            question_type=args.question_type,
            workers=args.workers,
        )
    )
    summary = _aggregate(entries)
    _print_summary(rid, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
