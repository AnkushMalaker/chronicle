#!/usr/bin/env python3
"""Ingest the pre-archive backup directories into the live database.

The ``.chronicle`` archive covers everything recorded since that format shipped. This
deployment's first six months are only in ``data/backups/backup_<timestamp>/`` — JSON
dumps of the API shape plus audio decoded back to WAV — so they cannot be restored with
``chronicle-data.sh import``. This converts and inserts them instead.

    python src/scripts/import_legacy_backups.py                     # dry run
    python src/scripts/import_legacy_backups.py --apply

What it does per conversation, in order:

1. Union every backup that still holds part of it (``services/legacy_backups.py``).
2. Skip anything already live — matched on ``conversation_id``, so re-running is safe.
3. Re-slice the concatenated WAVs into the 10-second chunks the metadata describes and
   re-encode each to Opus, which is the only form ``audio_chunks`` accepts.
4. Anchor ``captured_at`` with the one-time legacy-import policy. A conversation it declines to
   anchor is imported with null capture times and is therefore invisible to the
   timeline — deliberately, because a wrong anchor is worse than none.

Audio is not fabricated. A conversation whose bytes did not survive is imported with its
transcript, marked ``audio_archived``, and reported; nothing synthesises silence to fill
the gap.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.database import get_database
from backend.models.conversation import Conversation
from backend.models.job import _ensure_beanie_initialized
from backend.services.audio_claims import apply_audio_ranges
from backend.services.corpus_reconciliation import load_manifest
from backend.services.legacy_backups import (
    LegacyConversation,
    load_corpus,
    resolve_legacy_capture_anchor,
)
from backend.utils.audio_chunk_utils import convert_audio_to_chunks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("import_legacy")

DEFAULT_ROOT = Path("/app/data/backups")

# Mirrors what the live database already records for these devices. Getting this wrong
# is not cosmetic: ``data_purpose == "annotation"`` is what keeps mined speaker clips
# out of the timeline and out of memory, and an unmarked mining clip becomes a fake
# episode on whatever day it was mined.
ANNOTATION_DEVICES = {"speaker-mining", "annotation-import"}
# A conversation is worth importing only if it carries something. The rest are failed
# transcriptions and silence: 410 of this deployment's 772 missing conversations have
# neither a transcript nor surviving audio.
EMPTY = "empty"
# How often to print the running wall-clock breakdown.
REPORT_EVERY = 5


def _report_timing(
    position: int,
    total: int,
    stats: Counter,
    spent: Counter,
    run_started: float,
) -> None:
    """Where the wall clock is going, while it is still going there.

    Three phases with three different fixes: ``read_wav`` is disk and WAV decode,
    ``encode_opus`` is one ffmpeg process per 10-second chunk, ``mongo_write`` is the
    insert. Knowing which dominates is the difference between raising ``--concurrency``,
    batching the inserts, and doing nothing.
    """
    elapsed = time.monotonic() - run_started
    tracked = sum(spent.values()) or 1e-9
    parts = "  ".join(
        f"{phase}={seconds:.1f}s ({100 * seconds / tracked:.0f}%)"
        for phase, seconds in sorted(spent.items(), key=lambda item: -item[1])
    )
    audio = stats["audio_seconds"]
    log.info(
        "[%d/%d] %.1fs elapsed | %s | %.2fs/record | %.0fx realtime",
        position,
        total,
        elapsed,
        parts,
        elapsed / position,
        audio / elapsed if elapsed else 0.0,
    )


@dataclass
class Decision:
    record: LegacyConversation
    action: str
    reason: str
    user_id: str
    pcm_bytes: int = 0
    transcript_record: LegacyConversation | None = None

    @property
    def conversation_id(self) -> str:
        return self.record.conversation_id


def _device(client_id: str) -> str:
    parts = (client_id or "").split("-", 1)
    return parts[1] if len(parts) > 1 else ""


def _conversation_document(
    record: LegacyConversation,
    user_id: str,
    *,
    audio_present: bool,
    transcript_record: LegacyConversation | None = None,
) -> Conversation:
    """Map a legacy API dump onto the current model.

    The dumps carry view fields the model has no room for (``transcript``, ``segments``,
    ``memory_count``, ``id``) and lack fields added since (``data_purpose``,
    ``memory_excluded``). Filtering to ``model_fields`` handles both directions, and
    everything the model gained is then set explicitly rather than defaulted.
    """
    payload: dict[str, Any] = {
        key: value
        for key, value in record.document.items()
        if key in Conversation.model_fields
        and key not in {"id", "revision_id", "audio_ranges"}
    }
    payload["user_id"] = user_id
    if transcript_record is not None and transcript_record is not record:
        payload["transcript_versions"] = transcript_record.document.get(
            "transcript_versions", []
        )
        payload["active_transcript_version"] = transcript_record.document.get(
            "active_transcript_version"
        )
    device = _device(record.client_id)
    if device in ANNOTATION_DEVICES:
        payload["data_purpose"] = "annotation"
        payload["memory_excluded"] = True
        payload["memory_exclusion_reason"] = "annotation clip"
    if not audio_present:
        # The conversation outlived its audio. Saying so is the difference between
        # "nothing was recorded" and "the bytes are gone", and only the second is true.
        payload["audio_archived"] = True
        payload["archive_reason"] = "legacy_backup_audio_missing"
        payload["audio_chunks_count"] = 0
    return Conversation(**payload)


def _plan(
    corpus: Any,
    live_ids: set[str],
    remap: dict[str, str],
    *,
    include_empty: bool,
    require_audio: bool,
    known_users: set[str],
    canonical_by_source: dict[str, str] | None = None,
    transcript_source_by_canonical: dict[str, str] | None = None,
) -> list[Decision]:
    canonical_by_source = canonical_by_source or {}
    transcript_source_by_canonical = transcript_source_by_canonical or {}
    decisions: list[Decision] = []
    for record in corpus:
        canonical_id = canonical_by_source.get(
            record.conversation_id, record.conversation_id
        )
        if canonical_id != record.conversation_id:
            decisions.append(
                Decision(
                    record,
                    "skip",
                    f"reconciled_duplicate:{canonical_id}",
                    record.user_id,
                )
            )
            continue
        if record.conversation_id in live_ids:
            decisions.append(Decision(record, "skip", "already_live", record.user_id))
            continue
        user_id = remap.get(record.user_id, record.user_id)
        if user_id not in known_users:
            decisions.append(
                Decision(record, "skip", f"unknown_user:{record.user_id}", user_id)
            )
            continue
        has_text = bool(record.transcript)
        if not has_text and not record.has_audio and not include_empty:
            decisions.append(Decision(record, "skip", EMPTY, user_id))
            continue
        if require_audio and not record.has_audio:
            decisions.append(Decision(record, "skip", "no_audio", user_id))
            continue
        transcript_source_id = transcript_source_by_canonical.get(
            record.conversation_id
        )
        transcript_record = (
            corpus.conversations.get(transcript_source_id)
            if transcript_source_id
            else None
        )
        decisions.append(
            Decision(
                record,
                "import",
                "ok",
                user_id,
                transcript_record=transcript_record,
            )
        )
    return decisions


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument(
        "--remap-user",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="re-own a retired account's conversations",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="also import conversations with neither transcript nor audio",
    )
    parser.add_argument(
        "--require-audio",
        action="store_true",
        help="only conversations whose audio survived (the ones that reach the timeline)",
    )
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    parser.add_argument(
        "--reconciliation-manifest",
        type=Path,
        help="reviewed manifest; required for --apply",
    )
    args = parser.parse_args()

    remap = dict(pair.split("=", 1) for pair in args.remap_user)
    manifest = (
        load_manifest(args.reconciliation_manifest)
        if args.reconciliation_manifest
        else None
    )
    if args.apply and manifest is None:
        parser.error("--apply requires --reconciliation-manifest")
    if args.apply and manifest and not manifest.get("activation_allowed"):
        parser.error("reconciliation manifest has unresolved blockers")

    database = get_database()
    await database.command("ping")
    await _ensure_beanie_initialized()

    live_ids = set(await database["conversations"].distinct("conversation_id"))
    known_users = {
        str(user["_id"])
        for user in await database["users"].find({}, {"_id": 1}).to_list(length=None)
    }
    log.info("live: %d conversation(s), %d user(s)", len(live_ids), len(known_users))

    corpus = load_corpus(args.root)
    log.info("legacy corpus: %d conversation(s)", len(corpus))
    decisions = _plan(
        corpus,
        live_ids,
        remap,
        include_empty=args.include_empty,
        require_audio=args.require_audio,
        known_users=known_users,
        canonical_by_source=(manifest or {}).get("canonical_by_source"),
        transcript_source_by_canonical={
            decision["canonical_id"]: decision["selected_transcript_source_id"]
            for decision in (manifest or {}).get("decisions", [])
            if decision.get("selected_transcript_source_id")
        },
    )

    skipped = Counter(d.reason for d in decisions if d.action == "skip")
    importable = [d for d in decisions if d.action == "import"]
    if args.limit:
        importable = importable[: args.limit]

    print("\nplan")
    print("----")
    print(f"  import                 {len(importable)}")
    for reason, count in skipped.most_common():
        print(f"  skip {reason:<20} {count}")
    print(
        f"  with audio             {sum(1 for d in importable if d.record.has_audio)}"
    )
    print(
        f"  with transcript        {sum(1 for d in importable if d.record.transcript)}"
    )
    print(
        "  audio hours            "
        f"{sum(d.record.audio_duration for d in importable if d.record.has_audio)/3600:.1f}"
    )
    # Validate the mapping now rather than discovering a rejected field halfway
    # through a write: these dumps predate several model changes.
    invalid: Counter = Counter()
    for decision in importable:
        try:
            _conversation_document(
                decision.record,
                decision.user_id,
                audio_present=decision.record.has_audio,
                transcript_record=decision.transcript_record,
            )
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            invalid[type(error).__name__ + ": " + str(error).split("\n")[0]] += 1
    if invalid:
        print("\n  documents the current model rejects:")
        for message, count in invalid.most_common(10):
            print(f"    {count:>4}  {message[:140]}")
    else:
        print("  model mapping        all importable documents validate")

    if not args.apply:
        print("\ndry run — nothing written")
        return

    stats: Counter = Counter()
    anchors: Counter = Counter()
    imported: list[Conversation] = []
    # Wall-clock per phase. Reported every REPORT_EVERY records so the shape of the run
    # is visible while it runs rather than only in hindsight: the question is whether
    # ffmpeg, disk, or Mongo dominates, and each implies a different fix.
    spent: Counter = Counter()
    run_started = time.monotonic()

    for position, decision in enumerate(importable, start=1):
        record = decision.record
        pcm = b""
        sample_rate, channels = 16000, 1
        if record.has_audio:
            mark = time.monotonic()
            try:
                pcm, sample_rate, channels = record.read_pcm()
            except (
                Exception
            ) as error:  # noqa: BLE001 - one bad WAV must not stop the run
                log.warning("%s: unreadable audio (%s)", record.conversation_id, error)
                stats["audio_unreadable"] += 1
                pcm = b""
            spent["read_wav"] += time.monotonic() - mark

        # Build the semantic document first so the shared anchoring policy can inspect
        # its lineage/source. Raw audio is then imported as its own deterministic
        # capture; the Conversation claims the returned range and never owns chunks.
        conversation = _conversation_document(
            record,
            decision.user_id,
            audio_present=bool(pcm),
            transcript_record=decision.transcript_record,
        )
        capture = None
        if pcm:
            # The legacy WAV filename is an epoch-millisecond capture time and the only
            # surviving record of it; nothing inside Mongo can recover it later.
            anchor, reason = resolve_legacy_capture_anchor(record)
            anchors[reason] += 1
            if anchor is None:
                # The capture model requires an honest absolute identity. Import the
                # transcript stub, but do not invent a wall-clock position for bytes
                # whose capture time cannot be justified.
                conversation = _conversation_document(
                    record,
                    decision.user_id,
                    audio_present=False,
                    transcript_record=decision.transcript_record,
                )
                conversation.archive_reason = "legacy_backup_audio_unanchored"
                stats["audio_unanchored"] += 1
            else:
                mark = time.monotonic()
                capture = await convert_audio_to_chunks(
                    user_id=decision.user_id,
                    capture_source_id=record.client_id or "legacy-import",
                    audio_data=pcm,
                    sample_rate=sample_rate,
                    channels=channels,
                    sample_width=2,
                    captured_at=anchor,
                    capture_session_id=f"legacy-{record.conversation_id}",
                    origin="import",
                    external_source_id=f"legacy-backup:{record.conversation_id}",
                    data_purpose=conversation.data_purpose or "normal_capture",
                )
                spent["encode_opus"] += time.monotonic() - mark
                await apply_audio_ranges(
                    conversation, [capture.audio_range], save=False
                )
                stats["audio_seconds"] += int(capture.duration_seconds)
                stats["chunks"] += capture.chunk_count

        mark = time.monotonic()
        await conversation.insert()
        imported.append(conversation)
        spent["mongo_write"] += time.monotonic() - mark
        stats["conversations"] += 1

        if position % REPORT_EVERY == 0:
            _report_timing(position, len(importable), stats, spent, run_started)

    print("\nimported")
    print("--------")
    for key, value in sorted(stats.items()):
        print(f"  {key:<22} {value}")
    print("\ncapture-time anchors")
    for reason, count in anchors.most_common():
        print(f"  {reason:<28} {count}")
    unanchored = sum(
        count for reason, count in anchors.items() if reason.startswith("skipped")
    )
    print(
        f"\n{unanchored} conversation(s) left without capture times on purpose; "
        "they are searchable but not on the timeline."
    )


if __name__ == "__main__":
    asyncio.run(main())
