#!/usr/bin/env python3
"""Scan active transcript clocks and repair them against current conversation audio.

Severe mismatches are quarantined on their existing version and retranscribed through
the configured batch STT path. An explicit ``--provider-model`` can select a local model
without changing the deployment default. Every registry provider uses the content-hash
response cache, so retries reuse identical results. Provider tail overhangs of at most
one second are clipped locally without another provider call.

    python src/scripts/repair_transcript_timing.py          # scan only
    python src/scripts/repair_transcript_timing.py --apply  # repair
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

from backend.database import get_database
from backend.models.conversation import Conversation
from backend.models.job import _ensure_beanie_initialized
from backend.services.processing_artifacts import persist_timing_normalized_revision
from backend.services.transcript_integrity import (
    TranscriptTimingError,
    validate_and_normalize_transcript_timing,
)
from backend.utils.conversation_utils import mark_conversation_deleted
from backend.workers.transcription_jobs import transcribe_full_audio_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("repair_transcript_timing")

_transcribe = transcribe_full_audio_job.__wrapped__
BACKOFF_SECONDS = (5, 15, 45, 120)


def _active_version(document: dict[str, Any]) -> dict[str, Any] | None:
    versions = document.get("transcript_versions") or []
    active_id = document.get("active_transcript_version")
    return next(
        (version for version in versions if version.get("version_id") == active_id),
        versions[-1] if versions else None,
    )


def _speaker_source_version(document: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve an active speaker projection to the provider/timing source it copied."""

    current = _active_version(document)
    versions = {
        version.get("version_id"): version
        for version in document.get("transcript_versions") or []
        if version.get("version_id")
    }
    seen: set[str] = set()
    while current and (current.get("metadata") or {}).get("reprocessing_type") == (
        "speaker_diarization"
    ):
        version_id = str(current.get("version_id") or "")
        if not version_id or version_id in seen:
            return None
        seen.add(version_id)
        current = versions.get((current.get("metadata") or {}).get("source_version_id"))
    return current


def _is_rate_limit(error: BaseException) -> bool:
    message = str(error).lower()
    return "429" in message or "rate limit" in message


async def _scan(database: Any, ids: list[str]) -> tuple[int, list[dict], list[dict]]:
    query: dict[str, Any] = {
        "deleted": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
        "audio_total_duration": {"$gt": 0},
    }
    if ids:
        query["conversation_id"] = {"$in": ids}
    cursor = database["conversations"].find(
        query,
        projection={
            "conversation_id": 1,
            "title": 1,
            "audio_total_duration": 1,
            "active_transcript_version": 1,
            "transcript_versions": 1,
        },
    )
    scanned = 0
    severe: list[dict] = []
    edge_overhangs: list[dict] = []
    async for document in cursor:
        scanned += 1
        version = _speaker_source_version(document)
        if not version:
            continue
        segments = version.get("segments") or []
        words = version.get("words") or []
        if not segments and not words:
            continue
        duration = float(document.get("audio_total_duration") or 0.0)
        try:
            clean_segments, clean_words = validate_and_normalize_transcript_timing(
                segments, words, audio_duration=duration
            )
        except TranscriptTimingError as error:
            max_timing = max(
                [float(item.get("end", 0.0) or 0.0) for item in [*segments, *words]]
                or [0.0]
            )
            severe.append(
                {
                    "conversation_id": document["conversation_id"],
                    "title": document.get("title") or "",
                    "version_id": version["version_id"],
                    "duration": duration,
                    "max_timing": max_timing,
                    "error": error,
                }
            )
            continue
        if clean_segments != segments or clean_words != words:
            edge_overhangs.append(
                {
                    "conversation_id": document["conversation_id"],
                    "title": document.get("title") or "",
                    "version_id": version["version_id"],
                    "duration": duration,
                    "segments": clean_segments,
                    "words": clean_words,
                }
            )
    return scanned, severe, edge_overhangs


async def _mark_quarantined(database: Any, target: dict) -> None:
    error: TranscriptTimingError = target["error"]
    validation = {
        "status": "invalid",
        "code": error.code,
        "detail": str(error),
        "audio_duration": target["duration"],
        "max_timing": target["max_timing"],
        "detected_at": datetime.now(timezone.utc),
    }
    await database["conversations"].update_one(
        {"conversation_id": target["conversation_id"]},
        {
            "$set": {
                "transcript_integrity_error": f"{error.code}: {error}",
                "transcript_versions.$[version].metadata.timing_validation": validation,
            }
        },
        array_filters=[{"version.version_id": target["version_id"]}],
    )


async def _clip_edge_overhang(database: Any, target: dict) -> None:
    conversation = await Conversation.find_one(
        Conversation.conversation_id == target["conversation_id"]
    )
    if conversation is None:
        raise RuntimeError(f"Conversation {target['conversation_id']} disappeared")
    source = conversation.get_transcript_version(target["version_id"])
    if source is None:
        raise RuntimeError(
            f"Transcript version {target['version_id']} disappeared from "
            f"{target['conversation_id']}"
        )
    await persist_timing_normalized_revision(
        conversation,
        source,
        segments=target["segments"],
        words=target["words"],
        audio_duration=target["duration"],
    )
    await conversation.save()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--conversation-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--provider-model",
        help="Configured STT model name (for example stt-faster-whisper)",
    )
    args = parser.parse_args()

    database = get_database()
    await database.command("ping")
    await _ensure_beanie_initialized()
    scanned, severe, edge_overhangs = await _scan(database, args.conversation_id)
    if args.limit:
        severe = severe[: args.limit]
    log.info(
        "scanned=%d severe=%d edge_overhangs=%d",
        scanned,
        len(severe),
        len(edge_overhangs),
    )
    for target in severe:
        log.info(
            "SEVERE %s audio=%.3fs transcript=%.3fs %s",
            target["conversation_id"],
            target["duration"],
            target["max_timing"],
            target["title"],
        )
    if not args.apply:
        return

    for target in edge_overhangs:
        await _clip_edge_overhang(database, target)

    stats: Counter = Counter(edge_overhangs_clipped=len(edge_overhangs))
    started = time.monotonic()
    for position, target in enumerate(severe, start=1):
        await _mark_quarantined(database, target)
        for attempt, pause in enumerate((0, *BACKOFF_SECONDS)):
            if pause:
                stats["backoffs"] += 1
                await asyncio.sleep(pause)
            try:
                result = await _transcribe(
                    target["conversation_id"],
                    str(uuid.uuid4()),
                    "repair_transcript_timing",
                    provider_model_name=args.provider_model,
                )
                if result.get("skipped") and result.get("reason") == (
                    "empty_or_contentless_transcription"
                ):
                    await mark_conversation_deleted(
                        target["conversation_id"],
                        "no_speech_during_transcript_integrity_repair",
                    )
                    stats["no_speech_soft_deleted"] += 1
                else:
                    stats["retranscribed"] += 1
                break
            except Exception as error:  # noqa: BLE001 - continue the corpus repair
                if _is_rate_limit(error) and attempt < len(BACKOFF_SECONDS):
                    continue
                log.error("FAILED %s: %s", target["conversation_id"], str(error)[:300])
                stats["failed"] += 1
                break
        log.info("[%d/%d] %s", position, len(severe), dict(stats))
        await asyncio.sleep(0.5)

    rescanned, remaining, remaining_edges = await _scan(database, args.conversation_id)
    log.info(
        "DONE elapsed=%.1fs rescanned=%d remaining_severe=%d remaining_edges=%d stats=%s",
        time.monotonic() - started,
        rescanned,
        len(remaining),
        len(remaining_edges),
        dict(stats),
    )


if __name__ == "__main__":
    asyncio.run(main())
