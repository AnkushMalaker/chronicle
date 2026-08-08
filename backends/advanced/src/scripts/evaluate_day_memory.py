"""Measure how reliably a model records a day of timeline episodes into the vault.

Days that gain no vault content look like flakiness from the pipeline, which reports only
the last outcome. They are not: replaying three days three times each gave the same
outcome every time, and the two that recorded nothing said so for a stateable reason —
the retired per-observation curation had already written those episodes into the day
note. Measure before believing either story.

The vault copy matters. What is under test is writing a day into a note that already
holds 65-177 entries from that curation, so an empty vault would measure a different —
and easier — problem than the one in production.

Nothing here touches the live vault or the TimelineDay latches, and ``--pi-model``
swaps the model for this process only, so a frontier model can be priced against the
local one on identical days.

    uv run python scripts/evaluate_day_memory.py --date 2026-08-06 --trials 5 \
        --output /tmp/day-memory/qwen.csv
"""

import argparse
import asyncio
import csv
import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.memory_audit import MemoryAuditEntry
from advanced_omi_backend.models.timeline import (
    TimelineAnalysisRun,
    TimelineDay,
    TimelineEpisode,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.memory.agent.memory_agent import day_note_path
from advanced_omi_backend.services.memory.config import build_memory_config_from_env
from advanced_omi_backend.services.memory.providers.chronicle import MemoryService
from advanced_omi_backend.services.memory.vault_verify import verify_vault_changes
from advanced_omi_backend.services.timeline.memory import (
    _episode_transcripts,
    build_day_digest,
)

logger = logging.getLogger("evaluate_day_memory")

FIELDS = [
    "local_date",
    "trial",
    "executor",
    "model",
    "episodes",
    "conversational_episodes",
    "digest_chars",
    "episodes_in_digest",
    "episodes_dropped",
    "transcripts_trimmed",
    "day_note_chars_before",
    "day_note_entries_before",
    "wrote_day_note",
    "notes_touched",
    "rounds",
    "tool_calls",
    "agent_errors",
    "truncated",
    "stalled",
    "verify_findings",
    "elapsed_s",
    "outcome",
    "summary_chars",
    "summary_sha256",
]


@dataclass
class Capture:
    """Records what the agent returned, which add_day_memory does not surface."""

    results: list = field(default_factory=list)

    def wrap(self, agent_class):
        capture = self

        class Wrapped(agent_class):  # type: ignore[misc, valid-type]
            async def run(self, *args, **kwargs):
                result = await super().run(*args, **kwargs)
                capture.results.append(result)
                return result

        return Wrapped


async def _load_day(user_id: str, local_date: date, timezone_name: Optional[str]):
    query: dict[str, Any] = {
        "user_id": user_id,
        "local_date": datetime.combine(local_date, datetime.min.time()),
    }
    if timezone_name:
        query["timezone"] = timezone_name
    days = await TimelineDay.find(query).to_list()
    if not days:
        raise SystemExit(f"no TimelineDay for {local_date} (user {user_id})")
    # Stale rows exist for old timezones; prefer the most recently analysed.
    day = sorted(days, key=lambda d: d.revised_at, reverse=True)[0]
    episodes = await TimelineEpisode.find(
        TimelineEpisode.run_id == day.active_run_id,
        TimelineEpisode.user_id == day.user_id,
    ).to_list()
    return day, episodes


async def _trial(
    service: MemoryService,
    vault_source: Path,
    workdir: Path,
    day: TimelineDay,
    digest: str,
    trial: int,
) -> dict[str, Any]:
    root = workdir / f"trial-{trial:02d}"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(vault_source, root)

    day_rel = day_note_path(day.local_date.isoformat())
    existing = root / day_rel
    before_text = existing.read_text(encoding="utf-8") if existing.is_file() else ""
    before_snapshot = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in root.rglob("*.md")
        if p.is_file()
    }

    service.vault.user_root = lambda _uid, _root=root: _root  # type: ignore[assignment]
    capture = Capture()
    original = service._write_agent_class
    service._write_agent_class = lambda *a, **k: capture.wrap(original(*a, **k))  # type: ignore[assignment]

    started = time.perf_counter()
    try:
        success, touched = await service.add_day_memory(
            digest,
            day.local_date.isoformat(),
            day.user_id,
            source_date=datetime.combine(
                day.local_date, datetime.min.time()
            ).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - a crashed trial is a data point
        logger.exception("trial %d raised", trial)
        success, touched = False, []
        capture.results.append(None)
        _ = exc
    finally:
        service._write_agent_class = original  # type: ignore[assignment]
    elapsed = time.perf_counter() - started

    result = capture.results[-1] if capture.results else None
    findings = verify_vault_changes(root, before_snapshot)
    summary = (getattr(result, "summary", "") or "").strip()
    wrote_day = day_rel in (touched or [])
    if success and wrote_day:
        outcome = "written"
    elif success:
        outcome = "no_changes"
    else:
        outcome = "failed"

    return {
        "trial": trial,
        "day_note_chars_before": len(before_text),
        "day_note_entries_before": before_text.count("\n## "),
        "wrote_day_note": int(wrote_day),
        "notes_touched": len(touched or []),
        "rounds": getattr(result, "rounds", ""),
        "tool_calls": getattr(result, "tool_calls", ""),
        "agent_errors": len(getattr(result, "errors", []) or []),
        "truncated": int(bool(getattr(result, "truncated", False))),
        "stalled": int(bool(getattr(result, "stalled", False))),
        "verify_findings": len(findings),
        "elapsed_s": round(elapsed, 1),
        "outcome": outcome,
        "summary_chars": len(summary),
        "summary_sha256": hashlib.sha256(summary.encode()).hexdigest()[:16],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", action="append", required=True, dest="dates")
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--timezone", default=None)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/day-memory-eval"))
    parser.add_argument(
        "--keep-vaults",
        action="store_true",
        help="Keep each trial's vault copy for inspection.",
    )
    parser.add_argument(
        "--executor",
        default=None,
        choices=["direct", "codex", "pi"],
        help="Override memory.agents.write.backend for this run only.",
    )
    parser.add_argument(
        "--pi-model",
        default=None,
        help=(
            "Chronicle model-registry entry to run the pi executor against, "
            "overriding memory.backends.pi.model for this process only."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    await init_beanie(
        database=client.chronicle,
        document_models=[
            Conversation,
            User,
            # Without this every mutation logs "Failed to record vault audit", which
            # is the harness lacking the collection rather than the agent misbehaving.
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

    # The executors read the model from the cached registry, not from MemoryConfig,
    # so an override has to land there.  This process only; config.yml is untouched.
    registry = get_models_registry()
    if registry is None:
        raise SystemExit("model registry unavailable")
    if args.pi_model:
        if args.pi_model not in registry.models:
            raise SystemExit(f"no model registry entry named {args.pi_model!r}")
        registry.memory.setdefault("backends", {}).setdefault("pi", {})[
            "model"
        ] = args.pi_model

    config = build_memory_config_from_env()
    if args.executor:
        config.write_agent_backend = args.executor
    # Recovery would silently substitute a different model and corrupt the comparison.
    config.write_recovery_backend = None
    service = MemoryService(config)
    await service._ensure_initialized()
    executor = config.write_agent_backend
    if executor == "pi":
        model = str(registry.memory["backends"]["pi"].get("model") or "")
    elif executor == "codex":
        model = str(
            (registry.memory.get("backends", {}).get("codex") or {}).get("model") or ""
        )
    else:
        model = str((config.llm_config or {}).get("model") or "")

    args.workdir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []

    for raw in args.dates:
        local_date = date.fromisoformat(raw)
        day, episodes = await _load_day(user_id, local_date, args.timezone)
        transcripts = await _episode_transcripts(episodes)
        digest, dropped = build_day_digest(
            episodes, day.local_date, day.timezone, transcripts
        )
        digest_path = args.workdir / f"digest-{raw}.md"
        digest_path.write_text(digest, encoding="utf-8")
        logger.info(
            "%s: %d episode(s), %d conversational, digest %d chars (%d dropped) -> %s",
            raw,
            len(episodes),
            sum(1 for e in episodes if e.conversational),
            len(digest),
            len(dropped),
            digest_path,
        )

        vault_source = service.vault.user_root(user_id)
        for trial in range(1, args.trials + 1):
            row = await _trial(service, vault_source, args.workdir, day, digest, trial)
            row.update(
                local_date=raw,
                executor=executor,
                model=model,
                episodes=len(episodes),
                conversational_episodes=sum(1 for e in episodes if e.conversational),
                digest_chars=len(digest),
                episodes_in_digest=digest.count("\n### "),
                episodes_dropped=sum(
                    1 for item in dropped if "transcripts trimmed" not in item
                ),
                transcripts_trimmed=int(
                    any("transcripts trimmed" in item for item in dropped)
                ),
            )
            rows.append(row)
            logger.info(
                "%s trial %d/%d -> %s (touched=%d rounds=%s tools=%s %.0fs)",
                raw,
                trial,
                args.trials,
                row["outcome"],
                row["notes_touched"],
                row["rounds"],
                row["tool_calls"],
                row["elapsed_s"],
            )
            # Restore for the next day's source snapshot.
            service.vault.user_root = MemoryService(config).vault.user_root
            if not args.keep_vaults:
                shutil.rmtree(args.workdir / f"trial-{trial:02d}", ignore_errors=True)

    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("wrote %d row(s) -> %s", len(rows), args.output)


if __name__ == "__main__":
    asyncio.run(main())
