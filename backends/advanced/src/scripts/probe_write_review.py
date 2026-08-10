"""Measure the review agent as a detector, on bullets whose verdict is known.

The reviewer's job is to catch what structural verification cannot: a well-formed
bullet re-recording a fact the vault already holds. Whether it can is an empirical
question, and the only honest way to ask it is to hand it writes whose right answer we
already know and count what it flags.

Cases are injected into a copy of the real vault — the notes' existing content is the
thing being compared against, so a synthetic vault would measure nothing. The source is
the day digest the writer would have been given.

    uv run python scripts/probe_write_review.py --date 2026-08-04 --cases cases.json

``cases.json`` is a list of ``{path, verdict, bullet}``, where ``verdict`` is
``redundant``, ``new``, or ``unsupported``.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.memory_audit import MemoryAuditEntry
from advanced_omi_backend.models.timeline import (
    TimelineAnalysisRun,
    TimelineDay,
    TimelineEpisode,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.memory.agent.review_agent import review_vault_write
from advanced_omi_backend.services.memory.config import build_memory_config_from_env
from advanced_omi_backend.services.memory.providers.chronicle import MemoryService
from advanced_omi_backend.services.timeline.memory import (
    _episode_transcripts,
    build_day_digest,
)
from scripts.evaluate_day_memory import _load_day

logger = logging.getLogger("probe")


def _inject(
    root: Path, cases: list[dict[str, Any]]
) -> tuple[dict[str, str], list[str]]:
    """Append each case's bullet under its note's ``## About``; snapshot what was there."""

    before: dict[str, str] = {}
    touched: list[str] = []
    for case in cases:
        rel = case["path"]
        path = root / rel
        text = path.read_text(encoding="utf-8")
        before.setdefault(rel, text)
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.strip().lower() == "## about":
                lines.insert(index + 1, case["bullet"])
                break
        else:
            raise SystemExit(f"{rel} has no '## About' section")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if rel not in touched:
            touched.append(rel)
    return before, touched


def _quotes(detail: str, bullet: str, run: int = 5) -> bool:
    """Whether ``detail`` quotes any ``run``-word stretch of ``bullet``."""

    def words(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    haystack = " ".join(words(detail))
    needle = words(bullet)
    return any(
        " ".join(needle[i : i + run]) in haystack for i in range(len(needle) - run + 1)
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--timezone", default=None)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/review-probe"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    await init_beanie(
        database=client.chronicle,
        document_models=[
            Conversation,
            User,
            MemoryAuditEntry,
            TimelineDay,
            TimelineEpisode,
            TimelineAnalysisRun,
        ],
    )

    user_id = args.user_id
    if not user_id:
        user = await User.find_one({})
        if user is None:
            raise SystemExit("no users in the database")
        user_id = str(user.id)

    service = MemoryService(build_memory_config_from_env())
    await service._ensure_initialized()

    local_date = date.fromisoformat(args.date)
    day, episodes = await _load_day(user_id, local_date, args.timezone)
    transcripts = await _episode_transcripts(episodes)
    digest, _dropped = build_day_digest(
        episodes, day.local_date, day.timezone, transcripts
    )

    vault_source = service.vault.user_root(user_id)
    args.workdir.mkdir(parents=True, exist_ok=True)
    digest_path = args.workdir / f"digest-{args.date}.md"
    digest_path.write_text(digest, encoding="utf-8")
    print(f"digest: {len(digest)} chars -> {digest_path}")
    if not args.cases.is_file():
        raise SystemExit(f"no cases file at {args.cases}; the digest is written above")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))

    tally: dict[str, list[int]] = {}
    for trial in range(1, args.trials + 1):
        root = args.workdir / f"trial-{trial}"
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(vault_source, root)
        before, touched = _inject(root, cases)

        result = await review_vault_write(
            root,
            source=digest,
            before=before,
            touched=touched,
            record="day",
        )
        # A list, not a dict keyed by (path, rule): four findings on one note collapse
        # to two under that key, and the discarded pair reads as the reviewer having
        # missed those cases. It had not.
        flagged = [(f.path, f.rule, f.detail) for f in result.findings]
        print(
            f"\n=== trial {trial}: reported={result.reported} "
            f"rounds={result.rounds} tool_calls={result.tool_calls} "
            f"read={len(result.notes_read)} findings={len(result.findings)}"
        )
        for warning in result.warnings:
            print(f"    ! {warning}")
        for line in result.trace:
            print(f"    | {line}")
        for case in cases:
            # Attribute by quotation, never by note: every case here lands in the same
            # note, so a note-level fallback labels all of them with the first finding
            # and reports nonsense. The reviewer is told to quote the line at fault, so
            # a shared word-shingle is the honest link between finding and case.
            hit = next(
                (
                    rule
                    for path, rule, detail in flagged
                    if path.casefold() == case["path"].casefold()
                    and _quotes(detail, case["bullet"])
                ),
                None,
            )
            called = hit or "clean"
            want = case["verdict"] if case["verdict"] != "new" else "clean"
            mark = "OK " if called == want else "MISS"
            tally.setdefault(f"{case['verdict']}", []).append(1 if mark == "OK " else 0)
            print(f"    {mark} want={want:<12} got={called:<12} {case['bullet'][:80]}")
        for path, rule, detail in flagged:
            print(f"    -> {path} [{rule}]: {detail[:200]}")

    print("\n=== detection rate by label")
    for label, hits in sorted(tally.items()):
        print(f"  {label:<12} {sum(hits)}/{len(hits)}")


if __name__ == "__main__":
    asyncio.run(main())
