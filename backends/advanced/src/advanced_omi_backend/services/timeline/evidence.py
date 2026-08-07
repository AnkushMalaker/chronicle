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
from .timezone import canonical_timezone

SCREEN_EVIDENCE_CONTINUITY_GAP = timedelta(minutes=20)


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
            "observation_scope": (
                "coarse_application_session" if row.kind == "observation" else None
            ),
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


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return utc(value)
    if not value:
        return None
    try:
        return utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _device_items(row: DeviceInputItem) -> list[TimelineEvidenceItem]:
    """Materialize supported screen intervals from one durable device row.

    Observation state is intentionally sparse: initial/novel/liveness samples prove
    continuity without storing every captured frame. A collector interruption can leave
    one observation open across a long wall-clock gap, however. Split those unsupported
    gaps here so historical rows cannot claim activity while a source was offline.
    Meeting intervals have their own liveness/closure state machine and remain intact.
    """

    base = _device_item(row)
    if row.kind not in {"activity", "observation"} or base.kind == "meeting":
        return [base]

    start = utc(row.captured_at)
    provisional = row.ended_at is None
    if provisional:
        supported_markers = [
            marker
            for source in (*row.samples, *row.frame_candidates)
            if (marker := _timestamp(source.get("captured_at"))) is not None
            and marker >= start
        ]
        end = max(supported_markers, default=start)
        if end > start:
            base.ended_at = end
            base.metadata["provisional_end"] = True
            base.metadata["provisional_end_source"] = "latest_screen_marker"
    else:
        end = utc(row.ended_at)
    if end <= start:
        return [base]

    markers = {start, end}
    for sample in row.samples:
        marker = _timestamp(sample.get("captured_at"))
        if marker is not None and start <= marker <= end:
            markers.add(marker)
    for candidate in row.frame_candidates:
        marker = _timestamp(candidate.get("captured_at"))
        if marker is not None and start <= marker <= end:
            markers.add(marker)

    ordered = sorted(markers)
    groups: list[list[datetime]] = [[ordered[0]]]
    for marker in ordered[1:]:
        if marker - groups[-1][-1] > SCREEN_EVIDENCE_CONTINUITY_GAP:
            groups.append([marker])
        else:
            groups[-1].append(marker)
    if len(groups) == 1:
        return [base]

    image_candidate = next(
        (
            candidate
            for candidate in row.frame_candidates
            if candidate.get("frame_id") == row.metadata.get("preview_frame_id")
        ),
        row.frame_candidates[0] if row.frame_candidates else None,
    )
    image_captured_at = (
        _timestamp(image_candidate.get("captured_at")) if image_candidate else None
    )
    result: list[TimelineEvidenceItem] = []
    for index, group in enumerate(groups):
        item = base.model_copy(deep=True)
        item.evidence_id = f"{base.evidence_id}:segment:{index}"
        item.started_at = group[0]
        item.ended_at = group[-1] if group[-1] > group[0] else None
        segment_samples = [
            sample
            for sample in row.samples
            if (marker := _timestamp(sample.get("captured_at"))) is not None
            and group[0] <= marker <= group[-1]
        ]
        segment_candidates = [
            candidate
            for candidate in row.frame_candidates
            if (marker := _timestamp(candidate.get("captured_at"))) is not None
            and group[0] <= marker <= group[-1]
        ]
        text_parts = [
            str(row.metadata.get(key) or "")
            for key in ("app_name", "window_name", "browser_url")
        ]
        if index == 0:
            text_parts.extend(
                str(row.metadata.get(key) or "") for key in ("text", "summary")
            )
        text_parts.extend(
            str(sample.get("text") or "") for sample in segment_samples[-8:]
        )
        item.excerpt = (
            " · ".join(part.strip() for part in text_parts if part.strip())[:6000]
            or None
        )
        item.content_hash = hashlib.sha256(
            f"{base.content_hash}:{item.started_at.isoformat()}:"
            f"{item.ended_at.isoformat() if item.ended_at else ''}".encode()
        ).hexdigest()
        item.metadata["continuity_segment"] = index
        item.metadata["continuity_segment_count"] = len(groups)
        item.metadata["continuity_marker_count"] = len(group)
        item.metadata["sample_count"] = len(segment_samples)
        item.metadata["sample_fingerprints"] = [
            sample.get("content_fingerprint") for sample in segment_samples[-12:]
        ]
        item.metadata["frame_candidates"] = segment_candidates
        segment_has_image = bool(item.image_filename) and (
            (image_captured_at is None and index == 0)
            or (
                image_captured_at is not None
                and group[0] <= image_captured_at <= group[-1]
            )
        )
        if segment_has_image:
            item.image_filename = f"{item.image_filename}-segment-{index}"
        else:
            item.image_filename = None
            item.ephemeral = False
            item.metadata["image_content_type"] = None
        result.append(item)
    return result


def _coalesce_application_evidence(
    items: list[TimelineEvidenceItem],
) -> list[TimelineEvidenceItem]:
    """Compact each capture source independently, then merge for the user.

    Mongo's user/timestamp index is descending, and multiple devices naturally
    interleave. Sorting by source stream before compaction prevents negative gaps and
    ensures one computer never changes another computer's screen boundaries.
    """

    ordered = sorted(
        items,
        key=lambda item: (
            item.source_id or "",
            str(item.metadata.get("source_kind") or ""),
            utc(item.started_at),
            item.evidence_id,
        ),
    )
    result: list[TimelineEvidenceItem] = []
    for item in ordered:
        source_kind = item.metadata.get("source_kind")
        app_key = (
            item.source_id,
            source_kind,
            item.metadata.get("app_name"),
            item.metadata.get("window_name"),
            item.metadata.get("browser_url"),
        )
        previous = result[-1] if result else None
        previous_key = (
            (
                previous.source_id,
                previous.metadata.get("source_kind"),
                previous.metadata.get("app_name"),
                previous.metadata.get("window_name"),
                previous.metadata.get("browser_url"),
            )
            if previous
            else None
        )
        previous_end = (
            utc(previous.ended_at or previous.started_at) if previous else None
        )
        gap = utc(item.started_at) - previous_end if previous_end is not None else None
        if (
            previous
            and source_kind in {"activity", "screen_context"}
            and app_key == previous_key
            and gap is not None
            and timedelta(0) <= gap <= timedelta(seconds=60)
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
    return sorted(result, key=lambda item: (utc(item.started_at), item.evidence_id))


def _clip_evidence_to_range(
    items: list[TimelineEvidenceItem], low: datetime, high: datetime
) -> list[TimelineEvidenceItem]:
    """Clip every source to the user's requested local-day range."""

    clipped: list[TimelineEvidenceItem] = []
    for original in items:
        start = utc(original.started_at)
        end = utc(original.ended_at) if original.ended_at else None
        if end is None or end <= start:
            if low <= start < high:
                item = original.model_copy(deep=True)
                item.started_at = start
                item.ended_at = None
                clipped.append(item)
            continue
        bounded_start = max(start, low)
        bounded_end = min(end, high)
        if bounded_end <= bounded_start:
            continue
        item = original.model_copy(deep=True)
        item.started_at = bounded_start
        item.ended_at = bounded_end
        if bounded_start != start or bounded_end != end:
            item.metadata["clipped_to_day"] = True
        clipped.append(item)
    return clipped


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
        # Speaker-attributed, so the agent can name who spoke rather than inferring it.
        # This replaces a parallel `segments` blob that repeated the same text with
        # per-segment timestamps — 119KB for one conversation, and the largest single
        # contributor to a workspace too big for the agent to read.
        excerpt=_attributed_transcript(version, transcript),
        content_hash=(version.version_id if version else None),
        metadata={
            "direction": direction,
            "conversation_id": conversation.conversation_id,
            "speakers": sorted(
                {
                    segment.speaker
                    for segment in (version.segments if version else [])
                    if segment.speaker
                }
            ),
        },
    )


def _attributed_transcript(version: Any, fallback: str) -> str:
    """`speaker: text` lines, falling back to the plain transcript when undiarized."""

    segments = version.segments if version else []
    if not segments:
        return fallback[:30000]
    lines: list[str] = []
    budget = 30000
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        line = f"{segment.speaker or 'unknown'}: {text}"
        if len(line) > budget:
            lines.append(line[:budget])
            break
        lines.append(line)
        budget -= len(line) + 1
    return "\n".join(lines) or fallback[:30000]


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
    timezone_name = canonical_timezone(timezone_name)
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
        # A deleted recording is not evidence. Without this an episode can cite —
        # and a promoted recording can point at — audio the user can no longer play,
        # which is what a duplicate sweep leaves behind.
        {"deleted": {"$ne": True}},
    ).to_list()

    device_evidence: list[TimelineEvidenceItem] = []
    images: dict[str, bytes] = {}
    for row in rows:
        items = _device_items(row)
        device_evidence.extend(items)
        if row.media_data:
            images.update(
                {
                    item.evidence_id: row.media_data
                    for item in items
                    if item.image_filename
                }
            )

    evidence = [_audio_item(span) for span in spans]
    evidence.extend(_coalesce_application_evidence(device_evidence))
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

    evidence = _clip_evidence_to_range(evidence, day_start, range_end)
    evidence.sort(key=lambda item: (item.started_at, item.evidence_id))
    evidence_ids = {item.evidence_id for item in evidence}
    images = {
        evidence_id: data
        for evidence_id, data in images.items()
        if evidence_id in evidence_ids
    }
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
    return manifest, images
