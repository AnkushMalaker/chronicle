#!/usr/bin/env python3
"""Replay a transcript canary set into a fresh vault with one memory executor.

This is intentionally separate from the rebuild path: it never touches Mongo, queues,
the live vault, or the audit ledger.  It is a small reproducible quality experiment for
comparing the direct tool-calling agent with the Codex CLI agent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

DEFAULT_PREFIXES = (
    "5c0b2333",
    "99e2a020",
    "7620c7b4",
    "ea282e40",
    "a4ed37ac",
    "19f5a281",
    "d0de4521",
    "fd3f7f7d",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", choices=("codex", "direct"), required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", help="Codex model override recorded in the manifest")
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        help="Codex reasoning-effort override recorded in the manifest",
    )
    parser.add_argument(
        "--conversation",
        action="append",
        dest="prefixes",
        help="conversation id or unique prefix; repeatable (defaults to audited 8)",
    )
    parser.add_argument(
        "--keep-output",
        action="store_true",
        help="reuse the output vault; default refuses a non-empty destination",
    )
    return parser.parse_args()


def _load_rows(dataset: Path, prefixes: tuple[str, ...]) -> list[dict]:
    rows = [
        json.loads(line) for line in dataset.read_text().splitlines() if line.strip()
    ]
    selected = []
    for prefix in prefixes:
        matches = [row for row in rows if row["conversation_id"].startswith(prefix)]
        if len(matches) != 1:
            raise SystemExit(
                f"{prefix!r} matched {len(matches)} conversations (expected 1)"
            )
        selected.append(matches[0])
    return selected


def _prepare_output(output: Path, keep: bool) -> None:
    if output.exists() and any(output.iterdir()) and not keep:
        raise SystemExit(
            f"refusing non-empty output {output}; remove it or pass --keep-output"
        )
    output.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def _isolated_vault_lock(*_args, **_kwargs):
    """No Redis dependency: this process is the sole writer of a throwaway vault."""
    yield


async def _run(args: argparse.Namespace) -> int:
    # The script lives at backends/advanced/scripts; make src imports work when invoked
    # directly without requiring an editable install.
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend / "src"))

    from advanced_omi_backend.services.memory import vault_lock
    from advanced_omi_backend.services.memory.agent import codex_agent
    from advanced_omi_backend.services.memory.agent.codex_agent import CodexMemoryAgent
    from advanced_omi_backend.services.memory.agent.memory_agent import MemoryAgent
    from advanced_omi_backend.services.memory.vault_scaffold import seed_vault_scaffold

    # Production uses Redis to serialize writers.  This evaluator owns a unique fresh
    # directory and runs sequentially, so requiring the application stack would add no
    # safety and would make the experiment needlessly hard to reproduce.
    vault_lock.vault_run_lock = _isolated_vault_lock
    vault_lock.vault_note_lock = _isolated_vault_lock
    configured = codex_agent._codex_settings() if args.executor == "codex" else {}
    if args.executor == "codex" and (args.model or args.reasoning_effort):
        configured.update(
            {
                key: value
                for key, value in {
                    "model": args.model,
                    "reasoning_effort": args.reasoning_effort,
                }.items()
                if value
            }
        )
        codex_agent._codex_settings = lambda: configured

    # Codex receives the vault as both subprocess cwd and --cd.  Resolve once so a
    # caller's relative path is not interpreted relative to itself by the CLI.
    args.output = args.output.resolve()
    args.dataset = args.dataset.resolve()
    prefixes = tuple(args.prefixes or DEFAULT_PREFIXES)
    rows = _load_rows(args.dataset, prefixes)
    _prepare_output(args.output, args.keep_output)
    seed_vault_scaffold(args.output)
    agent_type = CodexMemoryAgent if args.executor == "codex" else MemoryAgent

    manifest = {
        "executor": args.executor,
        "model": args.model
        or (configured.get("model") if args.executor == "codex" else None),
        "reasoning_effort": args.reasoning_effort
        or (configured.get("reasoning_effort") if args.executor == "codex" else None),
        "dataset": str(args.dataset.resolve()),
        "output": str(args.output.resolve()),
        "started_at_epoch": time.time(),
        "runs": [],
    }
    manifest_path = args.output / "evaluation-manifest.json"
    failures = 0
    for index, row in enumerate(rows, 1):
        conversation_id = row["conversation_id"]
        print(
            f"[{index}/{len(rows)}] {args.executor}: {conversation_id} ({row['n_chars']} chars)",
            flush=True,
        )
        started = time.perf_counter()
        result = await agent_type(args.output).run(
            row["transcript"],
            conversation_id,
            date=row.get("created_at"),
            duration_minutes=(row.get("duration_s") or 0) / 60,
            title=row.get("title"),
        )
        note = args.output / "Conversations" / f"{conversation_id}.md"
        ok = note.is_file() and not result.truncated and not result.stalled
        failures += int(not ok)
        manifest["runs"].append(
            {
                "conversation_id": conversation_id,
                "ok": ok,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "rounds": result.rounds,
                "tool_calls": result.tool_calls,
                "touched": result.touched,
                "removed": result.removed,
                "errors": result.errors,
                "truncated": result.truncated,
                "stalled": result.stalled,
                "summary": result.summary,
            }
        )
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    manifest["finished_at_epoch"] = time.time()
    manifest["failures"] = failures
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"vault: {args.output.resolve()}\nmanifest: {manifest_path.resolve()}")
    return int(bool(failures))


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
