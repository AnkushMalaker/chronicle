"""Plan a contiguous operational timeline from immutable audio-chunk identity."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ChunkTimelineUpdate:
    document_id: Any
    chunk_index: int
    start_time: float
    end_time: float


@dataclass(frozen=True)
class ChunkTimelinePlan:
    updates: list[ChunkTimelineUpdate]
    duplicate_ids: list[Any]
    duration: float


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def plan_contiguous_chunk_timeline(chunks: list[dict]) -> ChunkTimelinePlan:
    """Keep one chunk per capture instant and rebase survivors contiguously.

    ``captured_at`` is immutable identity; relative indexes and offsets are mutable
    views. Reconnect retries can write more than one chunk at the same capture instant.
    The longest copy contains the shorter retry's interval, so it is retained and the
    other copies are quarantined by the caller rather than destroyed.
    """

    by_capture: dict[datetime, list[dict]] = {}
    for chunk in chunks:
        captured_at = chunk.get("captured_at")
        if captured_at is None:
            raise ValueError(f"Chunk {chunk.get('_id')} has no captured_at anchor")
        by_capture.setdefault(_as_utc(captured_at), []).append(chunk)

    survivors: list[tuple[datetime, dict]] = []
    duplicate_ids: list[Any] = []
    for captured_at, candidates in by_capture.items():
        keep = max(candidates, key=lambda item: float(item.get("duration") or 0.0))
        survivors.append((captured_at, keep))
        duplicate_ids.extend(
            item["_id"] for item in candidates if item["_id"] != keep["_id"]
        )

    survivors.sort(key=lambda item: item[0])
    cursor = 0.0
    updates: list[ChunkTimelineUpdate] = []
    for index, (_, chunk) in enumerate(survivors):
        duration = float(chunk.get("duration") or 0.0)
        if duration <= 0:
            raise ValueError(
                f"Chunk {chunk.get('_id')} has invalid duration {duration}"
            )
        end = cursor + duration
        updates.append(
            ChunkTimelineUpdate(
                document_id=chunk["_id"],
                chunk_index=index,
                start_time=cursor,
                end_time=end,
            )
        )
        cursor = end

    return ChunkTimelinePlan(
        updates=updates,
        duplicate_ids=duplicate_ids,
        duration=cursor,
    )
