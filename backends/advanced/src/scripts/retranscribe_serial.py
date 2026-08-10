#!/usr/bin/env python3
"""Re-transcribe recordings one at a time, in-process, with 429 backoff.

The provider allows roughly one request in flight. RQ can enforce that with a
``depends_on`` chain, but measured on this deployment each link cost **13.6s** while the
job itself took **3-9s**: five of six workers idled and a third of the wall clock went to
dependency resolution. Enqueuing them independently instead lets the fleet run six at
once and the provider answers with HTTP 429 within seconds.

So the concurrency limit is real and the queue is the wrong place to enforce it. This
calls the same job function directly in a loop: strictly serial, no scheduling gap, and
a 429 backs off and retries rather than failing the recording.

    python src/scripts/retranscribe_serial.py                # dry run
    python src/scripts/retranscribe_serial.py --apply

Safe to re-run: it re-selects from the database each time, so anything already carrying a
Pulse transcript drops out of the set.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
from collections import Counter
from typing import Any

from advanced_omi_backend.database import get_database
from advanced_omi_backend.models.job import _ensure_beanie_initialized
from advanced_omi_backend.workers.transcription_jobs import transcribe_full_audio_job

# The RQ decorator turns the job into a *sync* callable that opens its own event loop,
# which cannot be awaited from inside one. `__wrapped__` is the original coroutine, so
# the whole run shares a single loop and a single set of Mongo connections.
_transcribe = transcribe_full_audio_job.__wrapped__

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("retranscribe_serial")

TARGET_PROVIDER = "smallest"
SKIP_PURPOSES = {"annotation"}
MIN_SECONDS = 1.0
# Backoff schedule for HTTP 429. The provider's own message says "try again in a few
# seconds", so start there and grow; a recording is only abandoned after all of these.
BACKOFF_SECONDS = (5, 15, 45, 120)
# Breathing room between successful calls. Cheap insurance against a burst limit that
# counts requests per window rather than concurrent connections.
PACE_SECONDS = 0.5


def _active_version(document: dict[str, Any]) -> dict[str, Any] | None:
    versions = document.get("transcript_versions") or []
    active = document.get("active_transcript_version")
    for version in versions:
        if version.get("version_id") == active:
            return version
    return versions[-1] if versions else None


def _is_rate_limit(error: BaseException) -> bool:
    text = str(error).lower()
    return "429" in text or "rate limit" in text


async def _select(database: Any, include_annotation: bool) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    cursor = database["conversations"].find(
        {"deleted": {"$ne": True}, "audio_chunks_count": {"$gt": 0}},
        projection={
            "conversation_id": 1,
            "user_id": 1,
            "data_purpose": 1,
            "audio_total_duration": 1,
            "transcript_versions": 1,
            "active_transcript_version": 1,
            "created_at": 1,
        },
    )
    async for document in cursor:
        if document.get("data_purpose") in SKIP_PURPOSES and not include_annotation:
            continue
        seconds = float(document.get("audio_total_duration") or 0.0)
        if seconds < MIN_SECONDS:
            continue
        provider = (_active_version(document) or {}).get("provider") or "NONE"
        if provider == TARGET_PROVIDER:
            continue
        targets.append(document)
    targets.sort(key=lambda item: item.get("created_at") or "")
    return targets


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-annotation", action="store_true")
    args = parser.parse_args()

    database = get_database()
    await database.command("ping")
    await _ensure_beanie_initialized()

    targets = await _select(database, args.include_annotation)
    if args.limit:
        targets = targets[: args.limit]
    minutes = sum(float(t.get("audio_total_duration") or 0) for t in targets) / 60
    log.info("%d recording(s), %.0f minutes", len(targets), minutes)
    if not args.apply:
        print("dry run — nothing transcribed")
        return

    stats: Counter = Counter()
    started = time.monotonic()
    for position, document in enumerate(targets, start=1):
        conversation_id = document["conversation_id"]
        for attempt, pause in enumerate((0, *BACKOFF_SECONDS)):
            if pause:
                stats["backoffs"] += 1
                await asyncio.sleep(pause)
            try:
                await _transcribe(
                    conversation_id, str(uuid.uuid4()), "retranscribe_pulse"
                )
                stats["ok"] += 1
                break
            except Exception as error:  # noqa: BLE001 - one bad recording must not stop
                if _is_rate_limit(error) and attempt < len(BACKOFF_SECONDS):
                    continue
                log.warning("%s failed: %s", conversation_id[:8], str(error)[:160])
                stats["failed"] += 1
                break
        await asyncio.sleep(PACE_SECONDS)
        if position % 10 == 0:
            elapsed = time.monotonic() - started
            log.info(
                "[%d/%d] %.0fs elapsed | %.1fs/recording | ok=%d failed=%d backoffs=%d",
                position,
                len(targets),
                elapsed,
                elapsed / position,
                stats["ok"],
                stats["failed"],
                stats["backoffs"],
            )

    log.info("DONE %s in %.0fs", dict(stats), time.monotonic() - started)


if __name__ == "__main__":
    asyncio.run(main())
