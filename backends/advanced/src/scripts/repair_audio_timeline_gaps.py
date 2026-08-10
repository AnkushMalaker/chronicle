#!/usr/bin/env python3
"""Repair active transcripts whose timed items have no backing audio chunk.

The repair rebuilds each conversation's mutable chunk index/start/end view from the
immutable ``captured_at`` anchors. Reconnect copies at the same capture instant are
soft-deleted (the longest copy is kept). The old transcript is marked invalid, then the
current audio is retranscribed through the production cached Smallest.ai path.

    python src/scripts/repair_audio_timeline_gaps.py          # scan only
    python src/scripts/repair_audio_timeline_gaps.py --apply
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

from pymongo import UpdateOne

from advanced_omi_backend.database import get_database
from advanced_omi_backend.models.job import _ensure_beanie_initialized
from advanced_omi_backend.services.audio_timeline_integrity import (
    plan_contiguous_chunk_timeline,
)
from advanced_omi_backend.services.observability import record_event_sync
from advanced_omi_backend.services.transcript_integrity import (
    TranscriptTimingError,
    validate_and_normalize_transcript_timing,
)
from advanced_omi_backend.workers.transcription_jobs import transcribe_full_audio_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("repair_audio_timeline_gaps")
_transcribe = transcribe_full_audio_job.__wrapped__
BACKOFF_SECONDS = (5, 15, 45, 120)


def _active_version(document: dict[str, Any]) -> dict[str, Any] | None:
    versions = document.get("transcript_versions") or []
    active_id = document.get("active_transcript_version")
    return next(
        (version for version in versions if version.get("version_id") == active_id),
        versions[-1] if versions else None,
    )


def _is_rate_limit(error: BaseException) -> bool:
    message = str(error).lower()
    return "429" in message or "rate limit" in message


async def _scan(database: Any, ids: list[str]) -> tuple[int, list[dict]]:
    query: dict[str, Any] = {
        "deleted": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
        "audio_total_duration": {"$gt": 0},
    }
    if ids:
        query["conversation_id"] = {"$in": ids}
    ranges: dict[str, list[tuple[float, float]]] = {}
    chunk_cursor = database["audio_chunks"].find(
        {"deleted": {"$ne": True}},
        {"conversation_id": 1, "start_time": 1, "end_time": 1},
    )
    async for chunk in chunk_cursor:
        ranges.setdefault(chunk["conversation_id"], []).append(
            (float(chunk["start_time"]), float(chunk["end_time"]))
        )

    scanned = 0
    targets: list[dict] = []
    cursor = database["conversations"].find(
        query,
        {
            "conversation_id": 1,
            "title": 1,
            "audio_total_duration": 1,
            "active_transcript_version": 1,
            "transcript_versions": 1,
            "user_id": 1,
            "client_id": 1,
        },
    )
    async for document in cursor:
        scanned += 1
        version = _active_version(document)
        if not version:
            continue
        segments = version.get("segments") or []
        words = version.get("words") or []
        if not segments and not words:
            continue
        try:
            validate_and_normalize_transcript_timing(
                segments,
                words,
                audio_duration=float(document.get("audio_total_duration") or 0.0),
                audio_ranges=ranges.get(document["conversation_id"], []),
            )
        except TranscriptTimingError as error:
            if error.code != "transcript_audio_gap":
                continue
            targets.append(
                {
                    "conversation_id": document["conversation_id"],
                    "title": document.get("title") or "",
                    "version_id": version["version_id"],
                    "user_id": str(document.get("user_id") or "") or None,
                    "client_id": document.get("client_id"),
                    "error": error,
                }
            )
    return scanned, targets


async def _repair_chunk_view(database: Any, target: dict) -> dict:
    conversation_id = target["conversation_id"]
    chunks = (
        await database["audio_chunks"]
        .find(
            {"conversation_id": conversation_id, "deleted": {"$ne": True}},
            {
                "_id": 1,
                "chunk_index": 1,
                "captured_at": 1,
                "duration": 1,
            },
        )
        .to_list(length=None)
    )
    plan = plan_contiguous_chunk_timeline(chunks)
    operations = [
        UpdateOne(
            {"_id": update.document_id},
            {
                "$set": {
                    "chunk_index": update.chunk_index,
                    "start_time": update.start_time,
                    "end_time": update.end_time,
                }
            },
        )
        for update in plan.updates
    ]
    now = datetime.now(timezone.utc)
    operations.extend(
        UpdateOne(
            {"_id": duplicate_id},
            {"$set": {"deleted": True, "deleted_at": now}},
        )
        for duplicate_id in plan.duplicate_ids
    )
    if operations:
        await database["audio_chunks"].bulk_write(operations)

    error: TranscriptTimingError = target["error"]
    reason = f"{error.code}: {error}"
    validation = {
        "status": "invalid",
        "code": error.code,
        "detail": str(error),
        "detected_at": now,
    }
    await database["conversations"].update_one(
        {"conversation_id": conversation_id},
        {
            "$set": {
                "audio_chunks_count": len(plan.updates),
                "audio_total_duration": plan.duration,
                "audio_integrity_error": None,
                "transcript_integrity_error": reason,
                "vad_analysis": None,
                "transcript_versions.$[version].metadata.timing_validation": validation,
            }
        },
        array_filters=[{"version.version_id": target["version_id"]}],
    )
    await database["waveforms"].delete_many({"conversation_id": conversation_id})
    record_event_sync(
        severity="error",
        category="data_integrity",
        source="repair_audio_timeline_gaps",
        title="Transcript had no backing audio chunks",
        detail=reason,
        user_id=target["user_id"],
        client_id=target["client_id"],
        conversation_id=conversation_id,
        metadata={
            **error.details,
            "surviving_chunks": len(plan.updates),
            "duplicate_chunks_quarantined": len(plan.duplicate_ids),
            "repaired_duration": plan.duration,
        },
        incident_key=f"transcript-integrity:{conversation_id}",
    )
    return {
        "duration": plan.duration,
        "chunks": len(plan.updates),
        "duplicates": len(plan.duplicate_ids),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--conversation-id", action="append", default=[])
    args = parser.parse_args()

    database = get_database()
    await database.command("ping")
    await _ensure_beanie_initialized()
    scanned, targets = await _scan(database, args.conversation_id)
    log.info("scanned=%d audio_gap_conversations=%d", scanned, len(targets))
    for target in targets:
        log.info("GAP %s %s", target["conversation_id"], target["title"])
    if not args.apply:
        return

    stats: Counter = Counter()
    started = time.monotonic()
    for position, target in enumerate(targets, 1):
        details = await _repair_chunk_view(database, target)
        stats["duplicates_quarantined"] += details["duplicates"]
        for attempt, pause in enumerate((0, *BACKOFF_SECONDS)):
            if pause:
                stats["backoffs"] += 1
                await asyncio.sleep(pause)
            try:
                await _transcribe(
                    target["conversation_id"],
                    str(uuid.uuid4()),
                    "repair_audio_timeline_gap",
                )
                stats["retranscribed"] += 1
                record_event_sync(
                    severity="info",
                    category="data_integrity",
                    source="repair_audio_timeline_gaps",
                    title="Transcript/audio timeline repaired",
                    user_id=target["user_id"],
                    client_id=target["client_id"],
                    conversation_id=target["conversation_id"],
                    metadata=details,
                    incident_key=f"transcript-integrity:{target['conversation_id']}",
                    resolves_incident=True,
                )
                break
            except Exception as error:  # noqa: BLE001 - continue the corpus repair
                if _is_rate_limit(error) and attempt < len(BACKOFF_SECONDS):
                    continue
                stats["failed"] += 1
                log.error("FAILED %s: %s", target["conversation_id"], str(error)[:300])
                break
        log.info("[%d/%d] %s %s", position, len(targets), details, dict(stats))
        await asyncio.sleep(0.5)

    rescanned, remaining = await _scan(database, args.conversation_id)
    log.info(
        "DONE elapsed=%.1fs rescanned=%d remaining=%d stats=%s",
        time.monotonic() - started,
        rescanned,
        len(remaining),
        dict(stats),
    )


if __name__ == "__main__":
    asyncio.run(main())
