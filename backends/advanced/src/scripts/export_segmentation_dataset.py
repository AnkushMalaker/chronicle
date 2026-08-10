#!/usr/bin/env python3
"""Export the timeline segmentation task as a self-contained eval set.

Segmentation is the step that decides what an episode *is*: it reads a day's evidence
(transcript spans, screen/application activity, manual memories) and emits bounded
episodes with a title, summary, kind, salience, and assertions. Chronicle runs it with
Codex/gpt-5.6-luna today; this dumps the exact inputs and the resulting outputs so the
same task can be replayed against another model without a Chronicle deployment.

    python src/scripts/export_segmentation_dataset.py --user-id <id> --output /app/data/backups/segmentation

Per settled local day it writes ``days/<date>/evidence.json`` (what the agent is shown,
regenerated from the same assembler the live path uses) and ``days/<date>/episodes.json``
(what the reference run produced). The shared prompt and output schema are written once
at the root, so a replay is: build_prompt + schema + evidence.json -> episodes.

Images are deliberately omitted. They are the bulk of the payload and the segmentation
decision is carried by text; ``evidence.json`` keeps the filename so a vision variant can
be wired up later.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from advanced_omi_backend.database import get_database
from advanced_omi_backend.models.job import _ensure_beanie_initialized
from advanced_omi_backend.models.timeline import TimelineEpisode
from advanced_omi_backend.services.timeline.evidence import assemble_day_evidence
from advanced_omi_backend.services.timeline.prompt import (
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    build_prompt,
)
from advanced_omi_backend.services.timeline.workspace import write_workspace

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("export_segmentation")

# Fields that make an episode a *prediction* rather than a database row. Storage keys
# (run_id, episode_id, _id) would let a scorer match on identity instead of content.
EPISODE_FIELDS = (
    "started_at",
    "ended_at",
    "kind",
    "title",
    "summary",
    "conversational",
    "salience",
    "confidence",
    "activity_mode",
    "entities",
    "attributes",
)


def _plain(value: Any) -> Any:
    """JSON-safe, and stable enough to diff between runs."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes omitted>"
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    return value


def _episode_payload(episode: TimelineEpisode) -> dict[str, Any]:
    row = {field: _plain(getattr(episode, field, None)) for field in EPISODE_FIELDS}
    row["assertions"] = [
        {
            "claim": a.claim,
            "role": a.role,
            "confidence": a.confidence,
            "evidence_ids": list(getattr(a, "evidence_ids", []) or []),
        }
        for a in (episode.assertions or [])
    ]
    # Which evidence the agent said it used. This is what makes grounding scorable:
    # an episode citing evidence outside its own bounds is a hallucinated span.
    row["evidence_ids"] = [
        getattr(ref, "evidence_id", None) for ref in (episode.evidence_refs or [])
    ]
    return row


async def export_segmentation(
    database: Any, user_id: str, root: Path, *, max_days: int = 0
) -> dict[str, Any]:
    """Write the whole eval set under ``root`` and return its index.

    Split out from ``main`` so the research bundle can embed the same set rather than
    shelling out to this script and hoping the layouts stay in step.
    """
    (root / "days").mkdir(parents=True, exist_ok=True)

    (root / "prompt.txt").write_text(build_prompt("episodes.json"), encoding="utf-8")
    (root / "output_schema.json").write_text(
        json.dumps(OUTPUT_SCHEMA, indent=2), encoding="utf-8"
    )

    day_rows = (
        await database["timeline_days"]
        .find({"user_id": user_id, "memory_state": "written"})
        .sort("local_date", 1)
        .to_list(length=None)
    )
    if max_days:
        day_rows = day_rows[:max_days]
    log.info("exporting %d day(s)", len(day_rows))

    index = []
    for row in day_rows:
        local_date = row["local_date"]
        if isinstance(local_date, datetime):
            local_date = local_date.date()
        timezone_name = row.get("timezone") or "UTC"

        manifest, _images = await assemble_day_evidence(
            user_id, local_date, timezone_name
        )
        episodes = (
            await TimelineEpisode.find(
                TimelineEpisode.user_id == user_id,
                TimelineEpisode.local_date == local_date,
                TimelineEpisode.run_id == row.get("active_run_id"),
            )
            .sort("+started_at")
            .to_list()
        )

        day_dir = root / "days" / local_date.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        evidence = _plain(manifest.model_dump())
        (day_dir / "evidence.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # The agent never sees evidence.json. It is given a windowed workspace, because
        # the flat evidence list reaches 7.3MB on a real day. Emitting the same layout
        # is what makes a replay comparable to the reference run rather than a
        # differently-shaped task that happens to use the same data.
        workspace_dir = day_dir / "workspace"
        if workspace_dir.exists():
            for path in sorted(workspace_dir.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            workspace_dir.rmdir()
        workspace_dir.mkdir(parents=True)
        write_workspace(workspace_dir, manifest)
        (day_dir / "episodes.json").write_text(
            json.dumps(
                [_episode_payload(e) for e in episodes], indent=2, ensure_ascii=False
            ),
            encoding="utf-8",
        )

        transcript_chars = sum(
            len(item.get("excerpt") or "") for item in evidence.get("evidence", [])
        )
        index.append(
            {
                "local_date": local_date.isoformat(),
                "timezone": timezone_name,
                "evidence_items": len(evidence.get("evidence", [])),
                "transcript_chars": transcript_chars,
                "episodes": len(episodes),
                "conversational_episodes": sum(1 for e in episodes if e.conversational),
            }
        )
        log.info(
            "%s: %d evidence item(s), %d char(s), %d episode(s)",
            local_date,
            index[-1]["evidence_items"],
            transcript_chars,
            len(episodes),
        )

    payload: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "reference_executor": "codex/gpt-5.6-luna",
        "days": index,
        "totals": {
            "days": len(index),
            "evidence_items": sum(int(d["evidence_items"]) for d in index),
            "transcript_chars": sum(int(d["transcript_chars"]) for d in index),
            "episodes": sum(int(d["episodes"]) for d in index),
        },
    }
    (root / "index.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("wrote %s", root)
    return payload


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-days", type=int, default=0, help="0 = all")
    args = parser.parse_args()

    database = get_database()
    await database.command("ping")
    # assemble_day_evidence queries Beanie Documents, and a CLI process has none of
    # them registered; without this every model attribute is a plain field and the
    # query raises AttributeError. Reuses the workers' initializer so the model list
    # cannot drift from theirs.
    await _ensure_beanie_initialized()
    await export_segmentation(
        database, args.user_id, args.output, max_days=args.max_days
    )


if __name__ == "__main__":
    asyncio.run(main())
