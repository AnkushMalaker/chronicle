#!/usr/bin/env python3
"""Audit capture-chunk deduplication in Chronicle archive chains.

The current archive format omits an ``audio_chunks`` document only when its canonical
BSON digest matches the verified base. Pass earlier archives to
``chronicle_data.py export --base-archive``; unchanged compressed audio bytes are not
stored again, while a metadata change on the same chunk ID remains restorable. This
utility reports the physical and inherited chunk IDs in existing archives and detects
accidental physical duplication.

It deliberately does not decode audio or compare Conversations.  A Conversation is a
mutable semantic claim and is not the identity of capture evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

from backend.services.data_archive import (
    ArchiveError,
    _iter_bson,
    collect_archived_audio_chunk_ids,
    verify_data_archive,
)


def _physical_audio_ids(path: Path, manifest: dict[str, Any]) -> set[str]:
    metadata = manifest["collections"].get("audio_chunks")
    if not metadata:
        return set()
    with zipfile.ZipFile(path, mode="r", allowZip64=True) as archive:
        with archive.open(metadata["member"], mode="r") as stream:
            return {
                str(document["_id"])
                for document in _iter_bson(stream)
                if document.get("_id") is not None
            }


def audit_archives(paths: list[Path]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    physically_seen: dict[str, str] = {}
    duplicate_physical_ids: dict[str, list[str]] = {}

    for supplied in paths:
        path = supplied.expanduser().resolve()
        manifest = verify_data_archive(path)
        physical = _physical_audio_ids(path, manifest)
        inherited = {
            str(value) for value in manifest.get("excluded_audio_chunk_ids", [])
        }
        overlap = physical & inherited
        if overlap:
            raise ArchiveError(
                f"{path.name} both stores and excludes {len(overlap)} audio chunks"
            )
        for chunk_id in physical:
            previous = physically_seen.setdefault(chunk_id, path.name)
            if previous != path.name:
                duplicate_physical_ids.setdefault(chunk_id, [previous]).append(
                    path.name
                )
        rows.append(
            {
                "archive": str(path),
                "created_at": manifest.get("created_at"),
                "physical_audio_chunks": len(physical),
                "inherited_audio_chunks": len(inherited),
                "cumulative_audio_chunks": len(physical | inherited),
                "base_archives": manifest.get("base_archives", []),
            }
        )

    cumulative, references = collect_archived_audio_chunk_ids(paths)
    return {
        "archives": rows,
        "unique_audio_chunks_covered": len(cumulative),
        "physical_audio_chunks": len(physically_seen),
        "duplicate_physical_chunk_count": len(duplicate_physical_ids),
        "duplicate_physical_chunks": duplicate_physical_ids,
        "verified_references": references,
    }


def _archive_paths(args: argparse.Namespace) -> list[Path]:
    paths = list(args.archive or [])
    if args.backup_dir:
        paths.extend(sorted(args.backup_dir.expanduser().glob("*.chronicle")))
    unique = list(dict.fromkeys(path.expanduser().resolve() for path in paths))
    if not unique:
        raise ArchiveError("No .chronicle archives supplied")
    return unique


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="*", type=Path)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Also audit every .chronicle file in this directory",
    )
    parser.add_argument("--report", type=Path, help="Write the report as JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = audit_archives(_archive_paths(args))
    except (ArchiveError, OSError) as error:
        print(f"Archive audit failed: {error}", file=sys.stderr)
        return 1

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 2 if report["duplicate_physical_chunk_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
