#!/usr/bin/env python3
"""Inventory what still exists in the pre-archive backup directories.

Answers the question an import has to answer first: of everything this deployment ever
recorded, what survives on disk, in what form, and how much of it is missing from the
live database. Reads only — nothing here writes to Mongo or to the backups.

    python src/scripts/legacy_backup_report.py                       # summary
    python src/scripts/legacy_backup_report.py --json out.json       # full inventory

With ``--live`` it additionally queries the database and separates what is already
ingested from what is not, which is the list an import consumes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from advanced_omi_backend.services.legacy_backups import (
    LegacyConversation,
    discover_backups,
    load_corpus,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("legacy_report")

DEFAULT_ROOT = Path("/app/data/backups")


def _row(record: LegacyConversation) -> dict[str, Any]:
    created = record.created_at
    return {
        "conversation_id": record.conversation_id,
        "user_id": record.user_id,
        "client_id": record.client_id,
        "created_at": created.isoformat() if created else None,
        "local_date": created.date().isoformat() if created else None,
        "deleted": record.deleted,
        "deletion_reason": record.document.get("deletion_reason"),
        "title": record.document.get("title"),
        "transcript_chars": len(record.transcript),
        "transcript_versions": len(record.document.get("transcript_versions") or []),
        "segments": len((record.active_version or {}).get("segments") or []),
        "provider": (record.active_version or {}).get("provider"),
        "diarized": bool((record.active_version or {}).get("diarization_source")),
        "chunks": len(record.chunks),
        "audio_duration": round(record.audio_duration, 1),
        "has_audio": record.has_audio,
        "audio_source": (
            "chunk_wavs"
            if record.audio_dir is not None
            else ("legacy_wav" if record.legacy_wav else None)
        ),
        "audio_backup": record.audio_backup or record.legacy_wav_backup,
        "document_backup": record.document_backup,
        "chunks_backup": record.chunks_backup,
        "legacy_wav_captured_at": (
            record.legacy_wav_captured_at.isoformat()
            if record.legacy_wav_captured_at
            else None
        ),
        "seen_in": sorted(set(record.seen_in)),
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dates = sorted(row["local_date"] for row in rows if row["local_date"])
    with_text = [row for row in rows if row["transcript_chars"] > 0]
    return {
        "conversations": len(rows),
        "with_transcript": len(with_text),
        "transcript_chars": sum(row["transcript_chars"] for row in rows),
        "with_audio": sum(1 for row in rows if row["has_audio"]),
        "audio_hours": round(
            sum(row["audio_duration"] for row in rows if row["has_audio"]) / 3600, 1
        ),
        "diarized": sum(1 for row in rows if row["diarized"]),
        "soft_deleted": sum(1 for row in rows if row["deleted"]),
        "distinct_days": len({row["local_date"] for row in rows if row["local_date"]}),
        "first_day": dates[0] if dates else None,
        "last_day": dates[-1] if dates else None,
        "audio_sources": dict(
            Counter(row["audio_source"] for row in rows if row["audio_source"])
        ),
        "clients": dict(Counter(row["client_id"] for row in rows).most_common(10)),
    }


def _print_table(title: str, summary: dict[str, Any]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for key, value in summary.items():
        print(f"  {key:<20} {value}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--json", type=Path, help="write the full per-conversation rows"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="query the database and split ingested from missing",
    )
    args = parser.parse_args()

    backups = discover_backups(args.root)
    if not backups:
        raise SystemExit(f"no backup_* directories under {args.root}")
    log.info("reading %d backup director(ies)", len(backups))
    corpus = load_corpus(args.root, backups=backups)
    rows = [_row(record) for record in corpus]

    print(f"\nbackups under {args.root}")
    for backup in backups:
        counts = {
            "conversations": len(backup.conversations()),
            "chunk_rows": len(backup.chunk_rows()),
            "audio_dirs": len(backup.audio_dirs()),
            "legacy_wavs": len(backup.legacy_wavs()),
        }
        print(f"  {backup.name}  " + "  ".join(f"{k}={v}" for k, v in counts.items()))

    _print_table("union across all backups", _summarize(rows))

    payload: dict[str, Any] = {
        "root": str(args.root),
        "backups": [backup.name for backup in backups],
        "union": _summarize(rows),
        "conversations": rows,
    }

    if args.live:
        # Imported here so the filesystem inventory runs without a database.
        from advanced_omi_backend.database import get_database

        database = get_database()
        await database.command("ping")
        live_ids = set(await database["conversations"].distinct("conversation_id"))
        missing = [row for row in rows if row["conversation_id"] not in live_ids]
        present = [row for row in rows if row["conversation_id"] in live_ids]
        payload["live_conversations"] = len(live_ids)
        payload["already_ingested"] = _summarize(present)
        payload["missing_from_live"] = _summarize(missing)
        payload["missing_conversation_ids"] = [
            row["conversation_id"] for row in missing
        ]
        _print_table("already in the live database", _summarize(present))
        _print_table("MISSING from the live database", _summarize(missing))

        by_day = Counter(row["local_date"] for row in missing if row["local_date"])
        print("\n  missing by month:")
        by_month = Counter(day[:7] for day in by_day.elements())
        for month, count in sorted(by_month.items()):
            print(f"    {month}  {count:>4} conversation(s)")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
