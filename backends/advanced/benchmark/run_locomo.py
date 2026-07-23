"""LoCoMo benchmark runner for Chronicle.

LoCoMo is 10 multi-session conversations between two named speakers, each with
~100-260 QA pairs. Unlike LongMemEval (1 question per haystack), the unit of
*ingestion* here is the conversation and the unit of *scoring* is the question —
we ingest each conversation once, then answer all of its questions. That makes
LoCoMo cheap (10 ingests) and a direct test of the vault-first bet: do named
speakers + structured notes + grep/agentic retrieval answer as well as the
FalkorDB hybrid index?

Because retrieval goes through ``MemoryServiceBase.search_memories`` and
ingestion through ``add_memory``, the *same* runner measures any provider /
toggle by flipping Chronicle's config:

  * ``chronicle`` + graph on   → FalkorDB hybrid (vector + BM25 + entity BFS)
  * ``chronicle`` + graph off  → vault-first (grep + vault-map prompt)
  * ``graphiti``               → temporal graph

Models are config, not flags. Point Chronicle's ``memory_extraction`` / default
``llm`` at the extraction+answer model (e.g. gemini-2.5-flash-lite) and register
an ``llm_judge`` operation at a stronger model for trustworthy grading — see
``judge.resolve_judge_model``.

Per-question lifecycle written to ``runs/<run_id>/progress.jsonl`` (fsynced):

    (sample ingested once)  → answered → judged → done   ↘ error

Resume: re-run with ``--run-id <id>``. Samples whose every (filtered) question
is already ``done`` are skipped without re-ingesting; a sample with any pending
question is cleaned + re-ingested, then only its pending questions are answered.

Examples
--------
    # Smoke: one conversation, default categories (1-4)
    python -m benchmark.run_locomo --limit 1

    # Full run, only temporal questions, 8-way concurrent answering
    python -m benchmark.run_locomo --category 2 --workers 8

    # Include the adversarial (category 5) questions too
    python -m benchmark.run_locomo --include-adversarial

    # Resume
    python -m benchmark.run_locomo --run-id 20260603-021500-ab12cd
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from advanced_omi_backend.llm_client import get_llm_client

from .judge import judge_answer, resolve_judge_model
from .locomo_ingest import ingest_locomo_session
from .locomo_loader import LocomoQuestion, LocomoSample, load_locomo
from .progress import JudgeCache, ProgressEntry, ProgressFile, open_run
from .retrieve import build_answer_prompt, retrieve_context
from .run_longmemeval import (
    _aggregate,
    _print_summary,
    _resolve_extraction_model,
    _RunningTally,
)

logger = logging.getLogger(__name__)


def _user_id_for(sample: LocomoSample) -> str:
    """Per-conversation namespace; cleanup is cheap so collisions don't matter."""
    return f"locomo-{sample.sample_id}"


def _bench_email(user_id: str) -> str:
    return f"{user_id}@bench.local"


async def _ingest_sample(sample: LocomoSample, user_id: str) -> int:
    """Cleanup + ingest every session of one conversation. Returns chunk count."""
    from .ingest import cleanup_user

    await cleanup_user(user_id)
    total = 0
    for session in sample.sessions:  # already chronological
        if not session.turns:
            continue
        _, count, success = await ingest_locomo_session(
            user_id=user_id, session=session, user_email=_bench_email(user_id)
        )
        total += count
        if not success:
            logger.warning(
                "ingest failed sample=%s session=%s", sample.sample_id, session.session_id
            )
    return total


async def _answer_question(
    q: LocomoQuestion,
    *,
    user_id: str,
    progress: ProgressFile,
    judge_cache: JudgeCache,
    extraction_model: str,
    sem: asyncio.Semaphore,
    tally: _RunningTally,
    tally_lock: asyncio.Lock,
    completed: list[ProgressEntry],
) -> None:
    async with sem:
        try:
            # Retrieval is async; the answer LLM call is sync so we offload it to
            # a thread to let concurrent questions overlap. The judge runs on the
            # loop so the shared JudgeCache / ProgressFile are touched single-
            # threaded (ProgressFile.append is lock-guarded; JudgeCache is not).
            context = await retrieve_context(query=q.question, user_id=user_id)
            prompt = build_answer_prompt(question=q.question, context=context, question_date=None)

            progress.append(
                ProgressEntry(
                    question_id=q.question_id,
                    status="answered",
                    user_id=user_id,
                    question_type=q.category_label,
                    extraction_model=extraction_model,
                )
            )
            client = get_llm_client()
            answer = (await asyncio.to_thread(client.generate, prompt=prompt)).strip()
            progress.update(q.question_id, status="answered", answer=answer)

            label, _raw, judge_model = judge_answer(
                question_id=q.question_id,
                question_type=q.judge_type,
                question=q.question,
                ground_truth=q.answer,
                answer=answer,
                abstention=q.is_abstention,
                cache=judge_cache,
            )
            progress.update(q.question_id, status="judged", score=label, judge_model=judge_model)
            final = progress.update(q.question_id, status="done")

            async with tally_lock:
                completed.append(final)
                tally.record(final, resumed=False)
        except Exception as exc:  # noqa: BLE001 — one bad question must not kill the run
            logger.exception("qid=%s failed: %s", q.question_id, exc)
            progress.append(
                ProgressEntry(
                    question_id=q.question_id,
                    status="error",
                    user_id=user_id,
                    question_type=q.category_label,
                    extraction_model=extraction_model,
                    error=traceback.format_exc(limit=2),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            )


async def _run(
    *,
    data_path: Optional[str],
    limit: Optional[int],
    include_adversarial: bool,
    category: Optional[int],
    run_id: Optional[str],
    runs_root: Path,
    workers: int,
) -> tuple[str, list[ProgressEntry]]:
    rid, run_dir, progress, cache = open_run(run_id, runs_root)
    print(
        f"# {'resuming ' if run_id else ''}run_id={rid}  run_dir={run_dir}",
        flush=True,
    )

    extraction_model = _resolve_extraction_model()
    logger.info("extraction_model=%s  judge_model=%s", extraction_model, resolve_judge_model())

    tally = _RunningTally()
    tally_lock = asyncio.Lock()
    completed: list[ProgressEntry] = []
    workers = max(1, workers)
    sem = asyncio.Semaphore(workers)

    samples = list(
        load_locomo(data_path, include_adversarial=include_adversarial, limit=limit)
    )
    for sample in samples:
        user_id = _user_id_for(sample)
        questions = [
            q for q in sample.questions if category is None or q.category == category
        ]
        if not questions:
            continue

        pending = [q for q in questions if not progress.is_done(q.question_id)]
        # Fold already-done questions into the tally/aggregate from progress.
        for q in questions:
            if progress.is_done(q.question_id):
                entry = progress.get(q.question_id)
                assert entry is not None
                completed.append(entry)
                tally.record(entry, resumed=True)

        if not pending:
            logger.info("sample=%s: all %d questions done, skipping", sample.sample_id, len(questions))
            continue

        t0 = time.perf_counter()
        chunks = await _ingest_sample(sample, user_id)
        logger.info(
            "sample=%s: ingested %d sessions -> %d chunks (%.1fs); answering %d/%d questions",
            sample.sample_id,
            len(sample.sessions),
            chunks,
            time.perf_counter() - t0,
            len(pending),
            len(questions),
        )

        await asyncio.gather(
            *(
                _answer_question(
                    q,
                    user_id=user_id,
                    progress=progress,
                    judge_cache=cache,
                    extraction_model=extraction_model,
                    sem=sem,
                    tally=tally,
                    tally_lock=tally_lock,
                    completed=completed,
                )
                for q in pending
            )
        )

    return rid, completed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LoCoMo runner for Chronicle")
    parser.add_argument(
        "--data",
        default=None,
        help="Path to locomo10.json. If omitted, downloads the official file to ~/.cache/locomo/.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap on number of conversations (smoke flag)"
    )
    parser.add_argument(
        "--include-adversarial",
        action="store_true",
        help="Include category-5 (adversarial/unanswerable) questions. Off by default (mem0 parity).",
    )
    parser.add_argument(
        "--category",
        type=int,
        default=None,
        choices=(1, 2, 3, 4, 5),
        help="Restrict to one LoCoMo category (1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial).",
    )
    parser.add_argument("--run-id", default=None, help="Resume a previous run; omit to start new")
    parser.add_argument("--runs-dir", default="runs", help="Where to put runs/<run_id>/ (default: ./runs)")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Questions answered concurrently within a conversation (independent "
            "reads on the same per-conversation store). 1 by default; raise once "
            "you've confirmed the active memory provider handles concurrent reads."
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
            data_path=args.data,
            limit=args.limit,
            include_adversarial=args.include_adversarial,
            category=args.category,
            run_id=args.run_id,
            runs_root=Path(args.runs_dir),
            workers=args.workers,
        )
    )
    summary = _aggregate(entries)
    _print_summary(rid, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
