#!/usr/bin/env python3
"""Run fresh speaker recognition over every active audio conversation.

This intentionally calls the production speaker job directly instead of the public
reprocess endpoint. The endpoint also creates memory and title jobs; a corpus scan only
needs speaker work and must not flood unrelated queues.

Four conversations run concurrently by default. Each conversation retains the client
module's own bounded per-segment concurrency, so the GPU receives useful parallel work
without an unbounded request storm.

    python src/scripts/reprocess_speakers_corpus.py             # dry run
    python src/scripts/reprocess_speakers_corpus.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from advanced_omi_backend.database import get_database
from advanced_omi_backend.models.job import _ensure_beanie_initialized
from advanced_omi_backend.utils.segment_utils import classify_segment_text
from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("reprocess_speakers_corpus")
# The production job is intentionally chatty per segment. Keep the corpus monitor at
# conversation-level progress while preserving warnings and errors from the pipeline.
logging.getLogger("advanced_omi_backend").setLevel(logging.WARNING)
logging.getLogger("rq").setLevel(logging.WARNING)

_recognise = recognise_speakers_job.__wrapped__


def _active_version(document: dict[str, Any]) -> dict[str, Any] | None:
    versions = document.get("transcript_versions") or []
    active_id = document.get("active_transcript_version")
    return next(
        (version for version in versions if version.get("version_id") == active_id),
        versions[-1] if versions else None,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _has_speaker_work(version: dict[str, Any]) -> bool:
    segments = version.get("segments") or []
    if segments:
        return any(
            segment.get("segment_type", "speech") == "speech"
            or classify_segment_text(segment.get("text", "")) == "speech"
            for segment in segments
        )
    return bool(version.get("words"))


async def _select(
    database: Any, ids: list[str], skip_speaker_since: datetime | None
) -> list[dict[str, str]]:
    query: dict[str, Any] = {
        "deleted": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
        "audio_total_duration": {"$gt": 0},
        "active_transcript_version": {"$ne": None},
    }
    if ids:
        query["conversation_id"] = {"$in": ids}
    cursor = database["conversations"].find(
        query,
        projection={
            "conversation_id": 1,
            "active_transcript_version": 1,
            "transcript_versions.version_id": 1,
            "transcript_versions.transcript": 1,
            "transcript_versions.words": 1,
            "transcript_versions.segments": 1,
            "transcript_versions.created_at": 1,
            "transcript_versions.metadata.reprocessing_type": 1,
            "created_at": 1,
        },
    )
    targets: list[dict[str, str]] = []
    async for document in cursor:
        version = _active_version(document)
        if not version or not (version.get("transcript") or "").strip():
            continue
        if not _has_speaker_work(version):
            continue
        if (
            skip_speaker_since is not None
            and (version.get("metadata") or {}).get("reprocessing_type")
            == "speaker_diarization"
            and version.get("created_at") is not None
            and _as_utc(version["created_at"]) >= skip_speaker_since
        ):
            continue
        targets.append(
            {
                "conversation_id": document["conversation_id"],
                "source_version_id": version["version_id"],
                "created_at": str(document.get("created_at") or ""),
            }
        )
    targets.sort(key=lambda target: target["created_at"])
    return targets


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--conversation-id", action="append", default=[])
    parser.add_argument(
        "--skip-speaker-since",
        help="Resume marker: skip active speaker versions created at/after this ISO time",
    )
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")

    database = get_database()
    await database.command("ping")
    await _ensure_beanie_initialized()
    skip_speaker_since = None
    if args.skip_speaker_since:
        skip_speaker_since = _as_utc(
            datetime.fromisoformat(args.skip_speaker_since.replace("Z", "+00:00"))
        )
    targets = await _select(database, args.conversation_id, skip_speaker_since)
    if args.limit:
        targets = targets[: args.limit]
    log.info(
        "targets=%d outer_concurrency=%d speaker_only=true",
        len(targets),
        args.concurrency,
    )
    if not args.apply:
        return

    semaphore = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    stats: Counter = Counter()
    started = time.monotonic()

    async def process(position: int, target: dict[str, str]) -> None:
        async with semaphore:
            conversation_id = target["conversation_id"]
            try:
                result = await _recognise(
                    conversation_id,
                    str(uuid.uuid4()),
                    source_version_id=target["source_version_id"],
                )
                if result.get("success"):
                    stats["ok"] += 1
                    stats["identified_segments"] += int(
                        result.get("identified_segments", 0) or 0
                    )
                else:
                    stats["skipped"] += 1
                    log.warning(
                        "SKIP %s: %s",
                        conversation_id,
                        str(result.get("error") or result.get("skip_reason"))[:240],
                    )
            except Exception as error:  # noqa: BLE001 - finish the rest of the corpus
                stats["failed"] += 1
                log.error("FAIL %s: %s", conversation_id, str(error)[:300])
            finally:
                async with lock:
                    stats["completed"] += 1
                    completed = stats["completed"]
                    if completed % 10 == 0 or completed == len(targets):
                        elapsed = time.monotonic() - started
                        log.info(
                            "PROGRESS %d/%d %.1f%% elapsed=%.0fs rate=%.2f/min stats=%s",
                            completed,
                            len(targets),
                            completed * 100 / len(targets),
                            elapsed,
                            completed * 60 / elapsed,
                            dict(stats),
                        )

    await asyncio.gather(
        *(process(position, target) for position, target in enumerate(targets, 1))
    )
    log.info("DONE elapsed=%.1fs stats=%s", time.monotonic() - started, dict(stats))


if __name__ == "__main__":
    asyncio.run(main())
