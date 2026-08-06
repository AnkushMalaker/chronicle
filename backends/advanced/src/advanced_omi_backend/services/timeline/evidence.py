"""Deterministic evidence broker for one user's local day."""

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import DeviceInputItem
from advanced_omi_backend.models.timeline import AudioEvidenceSpan

from .contracts import (
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def day_bounds(local_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown IANA timezone: {timezone_name}") from error
    start = datetime.combine(local_date, time.min, tzinfo=zone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _evidence_id(kind: str, *parts: object) -> str:
    value = "\x1f".join(str(part) for part in parts)
    return f"{kind}:{hashlib.sha256(value.encode()).hexdigest()[:20]}"


def _overlaps(
    start: datetime, end: datetime | None, low: datetime, high: datetime
) -> bool:
    return utc(start) < high and utc(end or start) >= low


def _audio_item(span: AudioEvidenceSpan) -> TimelineEvidenceItem:
    role = "media_content" if span.direction == "output" else "uncertain"
    return TimelineEvidenceItem(
        evidence_id=f"audio_span:{span.id}",
        kind="audio_span",
        source_id=span.source_id,
        source_item_id=span.first_source_item_id,
        started_at=utc(span.started_at),
        ended_at=utc(span.ended_at),
        role=role,
        content_hash=span.source_range_hash,
        metadata={
            "direction": span.direction,
            "meeting_id": span.meeting_id,
            "state": span.state,
            "covered_seconds": span.covered_seconds,
            "missing_seconds": span.missing_seconds,
            "bucket_seconds": span.bucket_seconds,
            "coverage_fraction": span.coverage_fraction,
            "speech_fraction": span.speech_fraction,
            "acoustic_active_fraction": span.acoustic_active_fraction,
            "rms_dbfs": span.rms_dbfs,
            "peak_dbfs": span.peak_dbfs,
            "longest_no_speech_seconds": span.longest_no_speech_seconds,
            "conversation_id": span.conversation_id,
        },
    )


def _device_item(row: DeviceInputItem) -> TimelineEvidenceItem:
    if row.kind == "immich_memory":
        kind, role = "immich", "user_action"
    elif row.metadata.get("meeting_id"):
        kind, role = "meeting", "application_state"
    else:
        kind, role = "observation", "application_state"
    text_parts = [
        str(row.metadata.get(key) or "")
        for key in ("app_name", "window_name", "text", "summary")
    ]
    if row.samples:
        text_parts.extend(str(sample.get("text") or "") for sample in row.samples[-8:])
    excerpt = (
        " · ".join(part.strip() for part in text_parts if part.strip())[:6000] or None
    )
    metadata = {
        key: value
        for key, value in row.metadata.items()
        if key not in {"text", "summary"}
    }
    return TimelineEvidenceItem(
        evidence_id=f"{kind}:{row.id}",
        kind=kind,
        source_id=row.source_id,
        source_item_id=row.source_item_id,
        started_at=utc(row.captured_at),
        ended_at=utc(row.ended_at) if row.ended_at else None,
        role=role,
        excerpt=excerpt,
        content_hash=row.content_hash or row.curation_revision,
        ephemeral=bool(row.media_data),
        metadata={
            **metadata,
            "source_kind": row.kind,
            "sample_count": len(row.samples),
            "sample_fingerprints": [
                sample.get("content_fingerprint") for sample in row.samples[-12:]
            ],
            "frame_candidates": row.frame_candidates,
            "curation": row.curation,
            "image_content_type": row.media_content_type if row.media_data else None,
        },
        image_filename=f"{kind}-{row.id}" if row.media_data else None,
    )


def _coalesce_application_evidence(
    items: list[TimelineEvidenceItem],
) -> list[TimelineEvidenceItem]:
    """Collapse adjacent low-level app rows without imposing episode boundaries."""
    result: list[TimelineEvidenceItem] = []
    for item in items:
        source_kind = item.metadata.get("source_kind")
        app_key = (
            item.source_id,
            item.metadata.get("app_name"),
            item.metadata.get("window_name"),
        )
        previous = result[-1] if result else None
        previous_key = (
            (
                previous.source_id,
                previous.metadata.get("app_name"),
                previous.metadata.get("window_name"),
            )
            if previous
            else None
        )
        previous_end = (
            utc(previous.ended_at or previous.started_at) if previous else None
        )
        if (
            previous
            and source_kind in {"activity", "screen_context"}
            and previous.metadata.get("source_kind") == source_kind
            and app_key == previous_key
            and previous_end is not None
            and utc(item.started_at) - previous_end <= timedelta(seconds=60)
            and not previous.image_filename
            and not item.image_filename
        ):
            previous.ended_at = max(previous_end, utc(item.ended_at or item.started_at))
            excerpts = [value for value in (previous.excerpt, item.excerpt) if value]
            previous.excerpt = "\n".join(dict.fromkeys(excerpts))[-6000:] or None
            previous.metadata["coalesced_count"] = (
                int(previous.metadata.get("coalesced_count") or 1) + 1
            )
            previous.content_hash = hashlib.sha256(
                f"{previous.content_hash}:{item.content_hash}:{item.evidence_id}".encode()
            ).hexdigest()
            continue
        result.append(item.model_copy(deep=True))
    return result


def _transcript_item(conversation: Conversation) -> TimelineEvidenceItem | None:
    transcript = (conversation.transcript or "").strip()
    if not transcript:
        return None
    started_at = utc(conversation.created_at)
    ended_at = started_at + timedelta(seconds=conversation.audio_total_duration or 0)
    source_parts = (conversation.external_source_id or "").split(":")
    direction = source_parts[2] if len(source_parts) >= 3 else "unknown"
    role = "media_content" if direction == "output" else "uncertain"
    version = conversation.active_transcript
    return TimelineEvidenceItem(
        evidence_id=f"transcript:{conversation.conversation_id}",
        kind="transcript",
        source_item_id=conversation.conversation_id,
        started_at=started_at,
        ended_at=ended_at,
        role=role,
        excerpt=transcript[:30000],
        content_hash=(version.version_id if version else None),
        metadata={
            "direction": direction,
            "conversation_id": conversation.conversation_id,
            "segments": [
                {
                    "started_at": (
                        started_at + timedelta(seconds=segment.start)
                    ).isoformat(),
                    "ended_at": (
                        started_at + timedelta(seconds=segment.end)
                    ).isoformat(),
                    "speaker": segment.speaker,
                    "text": segment.text,
                }
                for segment in (version.segments if version else [])
            ],
        },
    )


def _window_items(
    started_at: datetime,
    ended_at: datetime,
    window_minutes: int,
    overlap_minutes: int,
    evidence: list[TimelineEvidenceItem],
) -> list[TimelineCoverageWindow]:
    width = timedelta(minutes=window_minutes)
    step = timedelta(minutes=window_minutes - overlap_minutes)
    windows: list[TimelineCoverageWindow] = []
    cursor = started_at
    while cursor < ended_at:
        window_end = min(ended_at, cursor + width)
        window_id = f"window:{cursor.isoformat()}:{window_end.isoformat()}"
        ids = [
            item.evidence_id
            for item in evidence
            if _overlaps(item.started_at, item.ended_at, cursor, window_end)
        ]
        windows.append(
            TimelineCoverageWindow(
                window_id=window_id,
                started_at=cursor,
                ended_at=window_end,
                evidence_ids=ids,
            )
        )
        cursor += step
    return windows


async def assemble_day_evidence(
    user_id: str,
    local_date: date,
    timezone_name: str,
    *,
    window_minutes: int = 20,
    overlap_minutes: int = 3,
    now: datetime | None = None,
) -> tuple[TimelineEvidenceManifest, dict[str, bytes]]:
    day_start, day_end = day_bounds(local_date, timezone_name)
    checked_at = utc(now or datetime.now(timezone.utc))
    range_end = (
        min(day_end, checked_at) if day_start <= checked_at < day_end else day_end
    )
    if range_end <= day_start:
        range_end = day_end

    spans = await AudioEvidenceSpan.find(
        AudioEvidenceSpan.user_id == user_id,
        AudioEvidenceSpan.started_at < range_end,
        AudioEvidenceSpan.ended_at >= day_start,
    ).to_list()
    rows = await DeviceInputItem.find(
        DeviceInputItem.user_id == user_id,
        DeviceInputItem.kind != "audio",
        DeviceInputItem.captured_at < range_end,
        {"$or": [{"ended_at": None}, {"ended_at": {"$gte": day_start}}]},
    ).to_list()
    conversations = await Conversation.find(
        Conversation.user_id == user_id,
        Conversation.external_source_type == "screenpipe",
        Conversation.created_at < range_end,
    ).to_list()

    evidence = [_audio_item(span) for span in spans]
    evidence.extend(_coalesce_application_evidence([_device_item(row) for row in rows]))
    for conversation in conversations:
        item = _transcript_item(conversation)
        if item and _overlaps(item.started_at, item.ended_at, day_start, range_end):
            evidence.append(item)

    for span in spans:
        if span.missing_seconds <= 0:
            continue
        evidence.append(
            TimelineEvidenceItem(
                evidence_id=_evidence_id("capture_gap", span.id, span.missing_seconds),
                kind="capture_gap",
                source_id=span.source_id,
                started_at=utc(span.started_at),
                ended_at=utc(span.ended_at),
                role="uncertain",
                metadata={"missing_seconds": span.missing_seconds, "within_span": True},
            )
        )

    evidence.sort(key=lambda item: (item.started_at, item.evidence_id))
    windows = _window_items(
        day_start, range_end, window_minutes, overlap_minutes, evidence
    )
    revision_payload = [
        {
            "id": item.evidence_id,
            "start": item.started_at.isoformat(),
            "end": item.ended_at.isoformat() if item.ended_at else None,
            "hash": item.content_hash,
            "excerpt": item.excerpt,
            "metadata": item.metadata,
        }
        for item in evidence
    ]
    revision = hashlib.sha256(
        json.dumps(
            revision_payload, sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()
    manifest = TimelineEvidenceManifest(
        user_id=user_id,
        local_date=local_date,
        timezone=timezone_name,
        started_at=day_start,
        ended_at=range_end,
        evidence_revision=revision,
        windows=windows,
        evidence=evidence,
    )
    images = {
        item.evidence_id: row.media_data
        for item, row in zip([_device_item(row) for row in rows], rows)
        if row.media_data
    }
    return manifest, images
