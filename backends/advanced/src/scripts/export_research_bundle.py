#!/usr/bin/env python3
"""Assemble one self-contained bundle for offline research on the memory pipeline.

The segmentation eval set answers "where should the boundaries be". It is not enough to
study the pipeline, because segmentation is only the first of two agents: the second
reads a day of episodes and writes the vault, and judging *that* needs the notes it
produced and the ledger of what each write changed. This packages all of it together
with the corpus inventory, so a run on a GPU box can be compared against the reference
end to end without a Chronicle deployment.

    python src/scripts/export_research_bundle.py --user-id <id> \
        --output /app/data/backups/chronicle-research

Contents:

    segmentation/    the eval set (prompt, schema, per-day workspace, reference episodes)
    vault/           the notes the memory agent wrote from those episodes
    episodes.jsonl   every episode, flat, for scoring across days
    timeline_days.jsonl   per-day run state: which run wrote it, attempts, errors
    memory_audit.jsonl    the write ledger -- one row per vault mutation
    legacy-inventory.json the full corpus inventory, if it has been generated

Audio is deliberately absent. It is two orders of magnitude larger than everything here
and none of these tasks are audio tasks; the transcripts are the input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

from export_segmentation_dataset import export_segmentation

from advanced_omi_backend.database import get_database
from advanced_omi_backend.models.job import _ensure_beanie_initialized

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("research_bundle")

VAULT_ROOT = Path("/app/data/conversation_docs")
# Collections worth shipping whole: each is small, and each records a decision the
# pipeline made rather than the data it made it from.
DUMPS = {
    "episodes.jsonl": "timeline_episodes",
    "timeline_days.jsonl": "timeline_days",
    "memory_audit.jsonl": "memory_audit",
}


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes omitted>"
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    return str(value) if type(value).__name__ == "ObjectId" else value


async def _dump(database: Any, collection: str, target: Path, user_id: str) -> int:
    rows = 0
    with target.open("w", encoding="utf-8") as handle:
        async for document in database[collection].find({"user_id": user_id}):
            document.pop("_id", None)
            handle.write(json.dumps(_plain(document), ensure_ascii=False) + "\n")
            rows += 1
    return rows


def _copy_vault(user_id: str, target: Path) -> int:
    source = VAULT_ROOT / user_id
    if not source.is_dir():
        return 0
    if target.exists():
        shutil.rmtree(target)
    # Sync markers are Syncthing's, not the vault's, and copying them into a bundle
    # someone later unpacks next to a real vault is a good way to confuse it.
    shutil.copytree(
        source, target, ignore=shutil.ignore_patterns(".stfolder", ".stignore")
    )
    return len(list(target.rglob("*.md")))


def _readme(stats: dict[str, Any]) -> str:
    return f"""# Chronicle research bundle

Generated {stats["generated_at"]} from the live deployment, user `{stats["user_id"]}`.

Two agents run per local day and this bundle carries both halves:

1. **Segmentation** decides what an episode *is* — it reads the day's evidence and emits
   bounded episodes with a title, summary, kind, salience, and grounded assertions.
   Reference outputs came from **{stats["reference_executor"]}**, prompt
   `{stats["prompt_version"]}`.
2. **The memory write** reads that day of episodes and edits the vault: one
   `Daily/<date>.md` note plus surgical edits to People/Topic/Category notes. Reference
   outputs came from **{stats["write_executor"]}**.

Neither reference is ground truth. Nobody hand-labelled any of this.

> **Real personal data**: transcripts, meetings, and screen activity belonging to the
> vault owner, with real names. Uploading it to a hosted GPU provider puts it on someone
> else's disk.

## Layout

| path | what it is |
|---|---|
| `segmentation/` | the segmentation task — see its own `README.md` |
| `vault/` | {stats["vault_notes"]} notes the write agent produced |
| `episodes.jsonl` | {stats["episode_rows"]} episodes, flat, for cross-day scoring — every run, including superseded ones, so it is a superset of the {stats["episodes"]} active-run episodes counted below |
| `timeline_days.jsonl` | {stats["timeline_days"]} per-day run records (state, attempts, errors) |
| `memory_audit.jsonl` | {stats["memory_audit"]} ledger rows — one per vault mutation |
| `legacy-inventory.json` | every conversation surviving in any backup, and whether it is live |

## Scale

{stats["days"]} settled days, {stats["evidence_items"]} evidence items,
{stats["transcript_chars"]:,} transcript characters, {stats["episodes"]} episodes
({stats["conversational_episodes"]} marked conversational).

## Two tasks, not one

**Boundary quality** is measured against `segmentation/days/<date>/episodes.json` —
temporal IoU, episode count, and whether each assertion's `evidence_ids` fall inside its
own episode's bounds. `segmentation/README.md` covers this in full.

**Write quality** is measured against `vault/` and `memory_audit.jsonl`. The ledger says
what each write touched, so a replay can be compared on *what changed*, not just on
prose. The two failure modes worth separating are a write that restates what the vault
already holds, and one that asserts something the day's episodes do not support.

## Corpus caveat

`legacy-inventory.json` lists {stats["legacy_conversations"]} conversations surviving in
the backup directories, of which **{stats["legacy_missing"]} are not in the live
database** — the deployment's first six months, which the days here therefore do not
cover. The days in this bundle start at {stats["first_day"]}. Treat the bundle as a
representative sample of the recent corpus, not as the whole history.
"""


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-days", type=int, default=0, help="0 = all")
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path("/app/data/backups/legacy-inventory.json"),
    )
    parser.add_argument("--zip", action="store_true", help="also write <output>.zip")
    args = parser.parse_args()

    database = get_database()
    await database.command("ping")
    await _ensure_beanie_initialized()

    root: Path = args.output
    root.mkdir(parents=True, exist_ok=True)

    log.info("exporting segmentation eval set")
    index = await export_segmentation(
        database, args.user_id, root / "segmentation", max_days=args.max_days
    )
    shutil.copy(
        Path(__file__).with_name("segmentation_eval_README.md"),
        root / "segmentation" / "README.md",
    )

    counts: dict[str, int] = {}
    for filename, collection in DUMPS.items():
        counts[collection] = await _dump(
            database, collection, root / filename, args.user_id
        )
        log.info("%s: %d row(s)", filename, counts[collection])

    vault_notes = _copy_vault(args.user_id, root / "vault")
    log.info("vault: %d note(s)", vault_notes)

    legacy = {"conversations": 0, "missing": 0}
    if args.inventory.is_file():
        shutil.copy(args.inventory, root / "legacy-inventory.json")
        payload = json.loads(args.inventory.read_text())
        legacy["conversations"] = payload.get("union", {}).get("conversations", 0)
        legacy["missing"] = payload.get("missing_from_live", {}).get("conversations", 0)

    days = index["days"]
    stats = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "user_id": args.user_id,
        "prompt_version": index["prompt_version"],
        "reference_executor": index["reference_executor"],
        "write_executor": "pi/qwen3.6-27b (local)",
        "days": index["totals"]["days"],
        "evidence_items": index["totals"]["evidence_items"],
        "transcript_chars": index["totals"]["transcript_chars"],
        "episodes": index["totals"]["episodes"],
        "conversational_episodes": sum(
            int(day["conversational_episodes"]) for day in days
        ),
        "first_day": days[0]["local_date"] if days else "n/a",
        "vault_notes": vault_notes,
        "episode_rows": counts.get("timeline_episodes", 0),
        "timeline_days": counts.get("timeline_days", 0),
        "memory_audit": counts.get("memory_audit", 0),
        "legacy_conversations": legacy["conversations"],
        "legacy_missing": legacy["missing"],
    }
    (root / "README.md").write_text(_readme(stats), encoding="utf-8")
    (root / "index.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    if args.zip:
        archive = shutil.make_archive(str(root), "zip", root_dir=root)
        log.info("wrote %s (%.1f MB)", archive, Path(archive).stat().st_size / 1e6)

    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
