#!/usr/bin/env python3
"""Find conversation audio that a previous backup already holds.

Backups store the same audio in two shapes: ``.chronicle`` archives keep the
original Opus bytes, while ``backup_*/audio/<conversation_id>/chunk_NNN.wav``
holds one minute of decoded PCM per file. Neither is byte-comparable with the
Opus documents in Mongo, so every source is reduced to the same canonical
fingerprint -- the SHA-256 of the conversation's decoded 16-bit PCM, in
chunk order -- and compared on that.

Matching is by content, not by conversation id: a conversation re-imported
under a new id still matches its earlier copy.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import wave
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Optional, cast

from advanced_omi_backend.database import get_database
from advanced_omi_backend.services.data_archive import (
    _iter_bson,
    _iter_unique_audio_chunks,
    _load_manifest,
)
from advanced_omi_backend.utils.audio_chunk_utils import decode_opus_to_pcm

DATA_DIR = Path("/app/data")
WAV_NAME = re.compile(r"^chunk_(\d+)\.wav$")


@dataclass
class Fingerprint:
    """A conversation's decoded-PCM identity plus what it was derived from."""

    conversation_id: str
    digest: str
    pcm_bytes: int
    parts: int
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    source: str = ""

    @property
    def seconds(self) -> float:
        if not self.sample_rate or not self.channels:
            return 0.0
        return self.pcm_bytes / (self.sample_rate * self.channels * 2)


@dataclass
class MatchReport:
    matched: dict[str, Fingerprint] = field(default_factory=dict)
    unmatched: dict[str, Fingerprint] = field(default_factory=dict)
    undecodable: dict[str, str] = field(default_factory=dict)


def _digest_of(parts: Iterable[bytes]) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    total = 0
    count = 0
    for part in parts:
        digest.update(part)
        total += len(part)
        count += 1
    return digest.hexdigest(), total, count


# --------------------------------------------------------------------------
# Prior backups: WAV directories
# --------------------------------------------------------------------------


def _wav_conversation_fingerprint(conv_dir: Path) -> Optional[Fingerprint]:
    """Fingerprint one ``audio/<conversation_id>/`` directory of 1-minute WAVs."""
    segments: list[tuple[int, Path]] = []
    for path in conv_dir.iterdir():
        match = WAV_NAME.match(path.name)
        if match and path.is_file():
            segments.append((int(match.group(1)), path))
    if not segments:
        return None
    segments.sort()

    digest = hashlib.sha256()
    total = 0
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    for _, path in segments:
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2:
                raise ValueError(f"{path}: expected 16-bit PCM")
            if sample_rate is None:
                sample_rate = handle.getframerate()
                channels = handle.getnchannels()
            elif (handle.getframerate(), handle.getnchannels()) != (
                sample_rate,
                channels,
            ):
                raise ValueError(f"{path}: format changes mid-conversation")
            frames = handle.readframes(handle.getnframes())
        digest.update(frames)
        total += len(frames)
    return Fingerprint(
        conversation_id=conv_dir.name,
        digest=digest.hexdigest(),
        pcm_bytes=total,
        parts=len(segments),
        sample_rate=sample_rate,
        channels=channels,
        source=f"wav:{conv_dir.parent.parent.name}",
    )


def index_wav_backups(
    backup_dir: Path, exclude: Optional[Path] = None
) -> list[Fingerprint]:
    """Fingerprint every conversation directory under ``backup_*/audio/``."""
    found: list[Fingerprint] = []
    for conv_dir in sorted(backup_dir.glob("backup_*/audio/*")):
        if not conv_dir.is_dir() or conv_dir.name.endswith(".partial"):
            continue
        if exclude is not None and conv_dir.parent.parent == exclude:
            continue
        fingerprint = _wav_conversation_fingerprint(conv_dir)
        if fingerprint is not None:
            found.append(fingerprint)
    return found


# --------------------------------------------------------------------------
# Prior backups: .chronicle archives
# --------------------------------------------------------------------------


async def _fingerprint_opus_groups(
    groups: dict[str, list[dict[str, Any]]],
    source: str,
    concurrency: int,
) -> tuple[list[Fingerprint], dict[str, str]]:
    """Decode each conversation's Opus chunks and fingerprint the PCM."""
    semaphore = asyncio.Semaphore(concurrency)
    failures: dict[str, str] = {}

    async def one(conversation_id: str, documents: list[dict[str, Any]]):
        async with semaphore:
            ordered = sorted(documents, key=lambda d: int(d.get("chunk_index", 0)))
            digest = hashlib.sha256()
            total = 0
            sample_rate = int(ordered[0].get("sample_rate", 16000))
            channels = int(ordered[0].get("channels", 1))
            for document in ordered:
                try:
                    pcm = await decode_opus_to_pcm(
                        bytes(document["audio_data"]),
                        sample_rate=int(document.get("sample_rate", sample_rate)),
                        channels=int(document.get("channels", channels)),
                    )
                except Exception as exc:  # decode failure must not claim a match
                    failures[conversation_id] = f"{type(exc).__name__}: {exc}"
                    return None
                digest.update(pcm)
                total += len(pcm)
            return Fingerprint(
                conversation_id=conversation_id,
                digest=digest.hexdigest(),
                pcm_bytes=total,
                parts=len(ordered),
                sample_rate=sample_rate,
                channels=channels,
                source=source,
            )

    results = await asyncio.gather(*(one(cid, docs) for cid, docs in groups.items()))
    return [item for item in results if item is not None], failures


async def index_chronicle_archives(
    backup_dir: Path, concurrency: int
) -> tuple[list[Fingerprint], dict[str, str]]:
    """Fingerprint every conversation carrying audio inside each archive."""
    found: list[Fingerprint] = []
    failures: dict[str, str] = {}
    for archive_path in sorted(backup_dir.glob("*.chronicle")):
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _load_manifest(archive)
            metadata = manifest["collections"].get("audio_chunks")
            if not metadata:
                continue
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            with archive.open(metadata["member"]) as stream:
                for document, duplicate in _iter_unique_audio_chunks(
                    _iter_bson(cast(BinaryIO, stream))
                ):
                    if duplicate:
                        continue
                    conversation_id = str(document.get("conversation_id", ""))
                    if conversation_id:
                        groups[conversation_id].append(document)
        fingerprints, archive_failures = await _fingerprint_opus_groups(
            groups, f"archive:{archive_path.name}", concurrency
        )
        found.extend(fingerprints)
        for conversation_id, reason in archive_failures.items():
            failures[f"{archive_path.name}:{conversation_id}"] = reason
    return found, failures


# --------------------------------------------------------------------------
# Live database
# --------------------------------------------------------------------------


async def select_conversations(
    database: Any,
    purposes: Optional[list[str]],
    conversation_ids: Optional[list[str]],
    limit: Optional[int],
) -> list[str]:
    query: dict[str, Any] = {}
    if conversation_ids:
        query["conversation_id"] = {"$in": conversation_ids}
    elif purposes:
        query["data_purpose"] = {"$in": purposes}
    cursor = database["conversations"].find(query, projection={"conversation_id": 1})
    selected = [
        str(doc["conversation_id"])
        async for doc in cursor
        if doc.get("conversation_id")
    ]
    selected.sort()
    if limit is not None:
        selected = selected[:limit]
    return selected


async def fingerprint_database(
    database: Any, conversation_ids: list[str], concurrency: int
) -> tuple[list[Fingerprint], dict[str, str]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cursor = database["audio_chunks"].find(
        {"conversation_id": {"$in": conversation_ids}},
        projection={
            "conversation_id": 1,
            "chunk_index": 1,
            "sample_rate": 1,
            "channels": 1,
            "audio_data": 1,
        },
    )
    async for document in cursor:
        conversation_id = str(document.get("conversation_id", ""))
        if conversation_id:
            groups[conversation_id].append(document)
    return await _fingerprint_opus_groups(groups, "database", concurrency)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def compare(
    database_fingerprints: list[Fingerprint],
    backup_fingerprints: list[Fingerprint],
    undecodable: dict[str, str],
) -> tuple[MatchReport, dict[str, list[Fingerprint]]]:
    by_digest: dict[str, list[Fingerprint]] = defaultdict(list)
    for fingerprint in backup_fingerprints:
        by_digest[fingerprint.digest].append(fingerprint)

    report = MatchReport(undecodable=dict(undecodable))
    for fingerprint in database_fingerprints:
        if fingerprint.digest in by_digest:
            report.matched[fingerprint.conversation_id] = fingerprint
        else:
            report.unmatched[fingerprint.conversation_id] = fingerprint
    return report, by_digest


def _render(
    report: MatchReport,
    by_digest: dict[str, list[Fingerprint]],
    selected: list[str],
    verbose: bool,
) -> dict[str, Any]:
    no_audio = sorted(
        set(selected)
        - set(report.matched)
        - set(report.unmatched)
        - set(report.undecodable)
    )
    print(f"Selected conversations:      {len(selected)}")
    print(f"  already in a prior backup: {len(report.matched)}")
    print(f"  not backed up             : {len(report.unmatched)}")
    print(f"  undecodable (kept)        : {len(report.undecodable)}")
    print(f"  no audio chunks           : {len(no_audio)}")

    matched_bytes = sum(f.pcm_bytes for f in report.matched.values())
    matched_hours = sum(f.seconds for f in report.matched.values()) / 3600
    print(
        f"\nDuplicate audio: {matched_hours:.1f} h "
        f"({matched_bytes / 1e9:.2f} GB decoded PCM)"
    )

    detail = []
    for conversation_id, fingerprint in sorted(report.matched.items()):
        sources = sorted({m.source for m in by_digest[fingerprint.digest]})
        ids = sorted({m.conversation_id for m in by_digest[fingerprint.digest]})
        renamed = ids != [conversation_id]
        detail.append(
            {
                "conversation_id": conversation_id,
                "digest": fingerprint.digest,
                "seconds": round(fingerprint.seconds, 2),
                "chunks": fingerprint.parts,
                "found_in": sources,
                "backup_conversation_ids": ids,
                "id_differs": renamed,
            }
        )
        if verbose:
            flag = "  (different id in backup)" if renamed else ""
            print(
                f"  {conversation_id}  {fingerprint.seconds:8.1f}s  "
                f"{fingerprint.digest[:16]}  {', '.join(sources)}{flag}"
            )

    for conversation_id, reason in sorted(report.undecodable.items()):
        print(f"  [undecodable] {conversation_id}: {reason}")

    return {
        "selected": len(selected),
        "matched": len(report.matched),
        "unmatched": len(report.unmatched),
        "undecodable": report.undecodable,
        "no_audio": no_audio,
        "duplicate_pcm_bytes": matched_bytes,
        "matches": detail,
        "unmatched_ids": sorted(report.unmatched),
    }


# --------------------------------------------------------------------------
# Smoke test
# --------------------------------------------------------------------------


async def run_smoke(database: Any, args: argparse.Namespace) -> int:
    """Prove the comparison on a handful of conversations before trusting it.

    A positive control must match a prior backup byte for byte, and a negative
    control must not, otherwise the fingerprint is not discriminating.
    """
    wav_index = index_wav_backups(args.backup_dir)
    by_id: dict[str, Fingerprint] = {}
    for fingerprint in wav_index:
        by_id.setdefault(fingerprint.conversation_id, fingerprint)

    selected = await select_conversations(
        database, args.purpose, args.conversation_id, None
    )
    positives = [cid for cid in selected if cid in by_id][: args.samples]
    negatives = [cid for cid in selected if cid not in by_id][:1]
    if not positives:
        print(
            "No selected conversation exists in a prior WAV backup; nothing to prove."
        )
        return 1

    fingerprints, failures = await fingerprint_database(
        database, positives + negatives, args.concurrency
    )
    live = {f.conversation_id: f for f in fingerprints}

    ok = True
    for conversation_id in positives:
        live_fingerprint = live.get(conversation_id)
        backup_fingerprint = by_id[conversation_id]
        print(f"\n=== positive control: {conversation_id}")
        if live_fingerprint is None:
            print(f"  FAIL: no live fingerprint ({failures.get(conversation_id)})")
            ok = False
            continue
        print(
            f"  live   : {live_fingerprint.parts} opus chunks -> "
            f"{live_fingerprint.pcm_bytes} B PCM "
            f"({live_fingerprint.seconds:.2f}s @ {live_fingerprint.sample_rate} Hz "
            f"x{live_fingerprint.channels})"
        )
        print(
            f"  backup : {backup_fingerprint.parts} wav files -> "
            f"{backup_fingerprint.pcm_bytes} B PCM "
            f"({backup_fingerprint.seconds:.2f}s @ "
            f"{backup_fingerprint.sample_rate} Hz x{backup_fingerprint.channels}) "
            f"[{backup_fingerprint.source}]"
        )
        print(f"  live   sha256: {live_fingerprint.digest}")
        print(f"  backup sha256: {backup_fingerprint.digest}")
        if live_fingerprint.digest == backup_fingerprint.digest:
            print("  MATCH: decoded PCM is byte-identical")
        else:
            print("  MISMATCH: audio differs, this conversation would be kept")
            if live_fingerprint.pcm_bytes != backup_fingerprint.pcm_bytes:
                print(
                    "    length differs by "
                    f"{live_fingerprint.pcm_bytes - backup_fingerprint.pcm_bytes} B"
                )
            ok = False

    for conversation_id in negatives:
        live_fingerprint = live.get(conversation_id)
        if live_fingerprint is None:
            continue
        print(f"\n=== negative control: {conversation_id}")
        collision = [f for f in wav_index if f.digest == live_fingerprint.digest]
        print(f"  live sha256: {live_fingerprint.digest}")
        if collision:
            print(
                "  NOTE: content matches backup conversation "
                f"{collision[0].conversation_id} under a different id"
            )
        else:
            print("  no backup holds this audio (correctly reported as new)")

    print("\nSmoke test:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------


async def run_scan(database: Any, args: argparse.Namespace) -> int:
    selected = await select_conversations(
        database, args.purpose, args.conversation_id, args.limit
    )
    print(f"Indexing prior backups under {args.backup_dir} ...")
    backup_fingerprints = index_wav_backups(args.backup_dir)
    print(f"  {len(backup_fingerprints)} conversations in WAV backups")
    archive_failures: dict[str, str] = {}
    if not args.skip_archives:
        archive_fingerprints, archive_failures = await index_chronicle_archives(
            args.backup_dir, args.concurrency
        )
        backup_fingerprints.extend(archive_fingerprints)
        print(f"  {len(archive_fingerprints)} conversations in .chronicle archives")
    for key, reason in archive_failures.items():
        print(f"  [archive decode failed] {key}: {reason}")

    print(f"Fingerprinting {len(selected)} live conversations ...")
    database_fingerprints, failures = await fingerprint_database(
        database, selected, args.concurrency
    )
    report, by_digest = compare(database_fingerprints, backup_fingerprints, failures)
    payload = _render(report, by_digest, selected, args.verbose)

    if args.exclude_list:
        args.exclude_list.parent.mkdir(parents=True, exist_ok=True)
        args.exclude_list.write_text(
            "".join(f"{cid}\n" for cid in sorted(report.matched))
        )
        print(f"\nExclusion list written to {args.exclude_list}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"Report written to {args.report}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("scan", run_scan), ("smoke", run_smoke)):
        sub = subparsers.add_parser(name)
        sub.add_argument("--purpose", action="append")
        sub.add_argument("--conversation-id", action="append")
        sub.add_argument("--backup-dir", type=Path, default=DATA_DIR / "backups")
        sub.add_argument("--concurrency", type=int, default=8)
        sub.set_defaults(handler=handler)
        if name == "scan":
            sub.add_argument("--limit", type=int)
            sub.add_argument("--skip-archives", action="store_true")
            sub.add_argument("--exclude-list", type=Path)
            sub.add_argument("--report", type=Path)
            sub.add_argument("--verbose", action="store_true")
        else:
            sub.add_argument("--samples", type=int, default=2)
    return parser


async def main() -> int:
    args = build_parser().parse_args()
    database = get_database()
    await database.command("ping")
    return await args.handler(database, args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
