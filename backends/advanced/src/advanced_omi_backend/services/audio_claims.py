"""Deep module for claiming and resolving immutable capture audio.

Callers work in conversation-relative seconds, while this module alone knows how
those presentation coordinates map onto absolute range claims and capture chunks.
No caller may recover audio by querying a conversation id on the chunk collection.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from beanie import PydanticObjectId
from beanie.operators import In

from advanced_omi_backend.models.audio_capture import (
    CAPTURE_CONTINUITY_TOLERANCE_SECONDS,
    ArtifactAudioSpan,
    AudioCaptureSession,
    AudioRangeRef,
    DiarizationArtifact,
    TranscriptArtifact,
    as_utc,
    bson_datetime,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import AudioEvidenceSpan, TimelineEpisode


class AudioClaimError(ValueError):
    """A semantic audio claim is missing, inconsistent, or unresolvable."""


_CAPTURE_CONTIGUITY_TOLERANCE = timedelta(seconds=CAPTURE_CONTINUITY_TOLERANCE_SECONDS)
_CAPTURE_COALESCE_TOLERANCE_SECONDS = 0.001


@dataclass(frozen=True)
class ClaimedChunk:
    """One chunk clipped to the part selected by an AudioRangeRef."""

    chunk: AudioChunkDocument
    range_id: str
    clip_start_seconds: float
    clip_end_seconds: float
    conversation_start_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.clip_end_seconds - self.clip_start_seconds


@dataclass(frozen=True)
class ConversationAudioLocation:
    """One playable semantic claim covering an absolute capture instant."""

    conversation_id: str
    offset_seconds: float
    chunk_id: str


def _chunk_end(chunk: AudioChunkDocument) -> datetime:
    return as_utc(chunk.captured_at) + timedelta(seconds=float(chunk.duration))


def range_duration(ranges: Sequence[AudioRangeRef]) -> float:
    return sum(item.duration_seconds for item in ranges)


def map_presentation_interval(
    ranges: Sequence[AudioRangeRef], start_seconds: float, end_seconds: float
) -> list[ArtifactAudioSpan]:
    """Map a gap-elided provider interval to physical absolute-time pieces.

    Ordered claims may have real gaps or overlapping wall-clock spans (for example,
    two concatenated capture channels). Mapping only the two endpoints can therefore
    create a backward absolute interval. Intersecting with every presentation range
    preserves the provider interval without inventing continuity between ranges.

    Zero-duration STT evidence maps to one point span. Callers that require a positive
    interval, such as neural diarization, must reject it before calling this helper.
    """

    if not ranges:
        raise AudioClaimError("cannot map an interval without audio ranges")
    start = float(start_seconds)
    end = float(end_seconds)
    if not math.isfinite(start) or not math.isfinite(end):
        raise AudioClaimError("presentation interval offsets must be finite")
    if end < start:
        raise AudioClaimError("presentation interval cannot run backward")

    total = range_duration(ranges)
    tolerance = 0.001
    if start < -tolerance or end > total + tolerance:
        raise AudioClaimError(
            f"presentation interval {start}-{end} lies outside {total} seconds of audio"
        )
    start = min(max(start, 0.0), total)
    end = min(max(end, 0.0), total)

    if end == start:
        cursor = 0.0
        for index, audio_range in enumerate(ranges):
            next_cursor = cursor + audio_range.duration_seconds
            if start < next_cursor or index == len(ranges) - 1:
                local = min(max(start - cursor, 0.0), audio_range.duration_seconds)
                point = bson_datetime(
                    as_utc(audio_range.started_at) + timedelta(seconds=local)
                )
                return [
                    ArtifactAudioSpan(
                        audio_range_id=audio_range.range_id,
                        started_at=point,
                        ended_at=point,
                    )
                ]
            cursor = next_cursor
        raise AssertionError("point interval mapping fell through")

    spans: list[ArtifactAudioSpan] = []
    cursor = 0.0
    mapped_duration = 0.0
    for audio_range in ranges:
        next_cursor = cursor + audio_range.duration_seconds
        overlap_start = max(start, cursor)
        overlap_end = min(end, next_cursor)
        if overlap_end > overlap_start:
            local_start = overlap_start - cursor
            local_end = overlap_end - cursor
            spans.append(
                ArtifactAudioSpan(
                    audio_range_id=audio_range.range_id,
                    started_at=(
                        as_utc(audio_range.started_at) + timedelta(seconds=local_start)
                    ),
                    ended_at=(
                        as_utc(audio_range.started_at) + timedelta(seconds=local_end)
                    ),
                )
            )
            mapped_duration += overlap_end - overlap_start
        cursor = next_cursor
        if cursor >= end:
            break
    if not spans or abs(mapped_duration - (end - start)) > tolerance:
        raise AudioClaimError(
            f"presentation interval {start}-{end} could not be mapped completely"
        )
    return spans


def ordered_chunk_ids(ranges: Sequence[AudioRangeRef]) -> list[str]:
    return [chunk_id for item in ranges for chunk_id in item.chunk_ids]


async def load_chunks_by_id(chunk_ids: Sequence[str]) -> list[AudioChunkDocument]:
    """Load the requested chunks in exactly the caller-provided order."""
    if not chunk_ids:
        return []
    unique_ids = list(dict.fromkeys(chunk_ids))
    try:
        object_ids = [PydanticObjectId(chunk_id) for chunk_id in unique_ids]
        documents = await AudioChunkDocument.find(
            In(AudioChunkDocument.id, object_ids),
            AudioChunkDocument.deleted == False,  # noqa: E712 - Beanie expression
        ).to_list()
    except Exception as error:
        raise AudioClaimError(f"Invalid audio chunk id in claim: {error}") from error
    by_id = {str(document.id): document for document in documents}
    missing = [chunk_id for chunk_id in unique_ids if chunk_id not in by_id]
    if missing:
        raise AudioClaimError(f"Audio claim references missing chunk {missing[0]}")
    return [by_id[chunk_id] for chunk_id in chunk_ids]


async def resolve_audio_ranges(
    ranges: Sequence[AudioRangeRef],
) -> list[ClaimedChunk]:
    """Resolve ordered absolute claims into clipped, conversation-relative slices."""
    chunks = await load_chunks_by_id(ordered_chunk_ids(ranges))
    cursor = 0
    conversation_offset = 0.0
    resolved: list[ClaimedChunk] = []

    for audio_range in ranges:
        range_start = as_utc(audio_range.started_at)
        range_end = as_utc(audio_range.ended_at)
        previous_end: Optional[datetime] = None
        first_clip_start: Optional[datetime] = None
        last_clip_end: Optional[datetime] = None
        for _ in audio_range.chunk_ids:
            chunk = chunks[cursor]
            cursor += 1
            if chunk.capture_source_id != audio_range.capture_source_id:
                raise AudioClaimError(
                    f"Range {audio_range.range_id} crosses capture sources"
                )
            chunk_start = as_utc(chunk.captured_at)
            chunk_end = _chunk_end(chunk)
            clip_start = max(range_start, chunk_start)
            clip_end = min(range_end, chunk_end)
            if clip_end <= clip_start:
                raise AudioClaimError(
                    f"Range {audio_range.range_id} includes non-overlapping chunk {chunk.id}"
                )
            if (
                previous_end is not None
                and chunk_start - previous_end > _CAPTURE_CONTIGUITY_TOLERANCE
            ):
                raise AudioClaimError(
                    f"Range {audio_range.range_id} contains an audio gap"
                )
            resolved.append(
                ClaimedChunk(
                    chunk=chunk,
                    range_id=audio_range.range_id,
                    clip_start_seconds=(clip_start - chunk_start).total_seconds(),
                    clip_end_seconds=(clip_end - chunk_start).total_seconds(),
                    conversation_start_seconds=(
                        conversation_offset + (clip_start - range_start).total_seconds()
                    ),
                )
            )
            first_clip_start = first_clip_start or clip_start
            last_clip_end = clip_end
            previous_end = max(previous_end, chunk_end) if previous_end else chunk_end
        tolerance = _CAPTURE_CONTIGUITY_TOLERANCE
        if (
            first_clip_start is None
            or first_clip_start - range_start > tolerance
            or last_clip_end is None
            or range_end - last_clip_end > tolerance
        ):
            raise AudioClaimError(
                f"Range {audio_range.range_id} chunk ids do not cover its bounds"
            )
        conversation_offset += audio_range.duration_seconds

    return resolved


def _capture_range_id(
    capture_session_id: str,
    chunk_ids: Sequence[str],
    started_at: datetime,
    ended_at: datetime,
) -> str:
    """Build a stable identity for one exact physical capture island."""
    identity = ":".join(
        (
            "chronicle-capture-range-v1",
            capture_session_id,
            chunk_ids[0],
            chunk_ids[-1],
            bson_datetime(started_at).isoformat(),
            bson_datetime(ended_at).isoformat(),
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def _build_capture_ranges(
    *,
    capture_session_id: str,
    capture_source_id: str,
    time_basis: str,
    chunks: Sequence[AudioChunkDocument],
    started_at: datetime,
    ended_at: datetime,
) -> list[AudioRangeRef]:
    """Split a capture window into gap-free physical islands.

    Capture sessions survive device reconnects and therefore need not be continuous
    in wall-clock time. Presentation time concatenates these islands; it must not
    claim that a reconnect gap contains audio.
    """
    clipped: list[tuple[AudioChunkDocument, datetime, datetime]] = []
    for chunk in chunks:
        chunk_start = as_utc(chunk.captured_at)
        chunk_end = _chunk_end(chunk)
        clip_start = max(started_at, chunk_start)
        clip_end = min(ended_at, chunk_end)
        if clip_end > clip_start:
            clipped.append((chunk, clip_start, clip_end))

    groups: list[list[tuple[AudioChunkDocument, datetime, datetime]]] = []
    current: list[tuple[AudioChunkDocument, datetime, datetime]] = []
    current_audio_seconds = 0.0
    for item in clipped:
        chunk, clip_start, clip_end = item
        clip_seconds = (clip_end - clip_start).total_seconds()
        if current:
            group_start = current[0][1]
            previous_end = current[-1][2]
            combined_audio_seconds = current_audio_seconds + clip_seconds
            combined_wall_seconds = (clip_end - group_start).total_seconds()
            boundary_error = abs((clip_start - previous_end).total_seconds())
            cumulative_error = abs(combined_wall_seconds - combined_audio_seconds)
            can_coalesce = (
                boundary_error <= _CAPTURE_COALESCE_TOLERANCE_SECONDS
                and cumulative_error <= _CAPTURE_COALESCE_TOLERANCE_SECONDS
            )
            if not can_coalesce:
                groups.append(current)
                current = []
                current_audio_seconds = 0.0
        current.append(item)
        current_audio_seconds += clip_seconds
    if current:
        groups.append(current)

    ranges: list[AudioRangeRef] = []
    for group in groups:
        range_start = group[0][1]
        range_end = group[-1][2]
        chunk_ids = [str(chunk.id) for chunk, _, _ in group]
        ranges.append(
            AudioRangeRef(
                range_id=_capture_range_id(
                    capture_session_id,
                    chunk_ids,
                    range_start,
                    range_end,
                ),
                capture_source_id=capture_source_id,
                time_basis=time_basis,
                capture_session_ids=[capture_session_id],
                chunk_ids=chunk_ids,
                started_at=range_start,
                ended_at=range_end,
            )
        )
    return ranges


async def claim_capture_window(
    capture_session_id: str,
    started_at: datetime,
    ended_at: datetime,
) -> list[AudioRangeRef]:
    """Create ordered, gap-free claims over a persisted absolute window."""
    started_at, ended_at = as_utc(started_at), as_utc(ended_at)
    if ended_at <= started_at:
        raise AudioClaimError("capture claim must have positive duration")

    session = await AudioCaptureSession.find_one(
        AudioCaptureSession.capture_session_id == capture_session_id
    )
    if session is None:
        raise AudioClaimError(f"Capture session {capture_session_id} does not exist")

    candidates = (
        await AudioChunkDocument.find(
            AudioChunkDocument.capture_session_id == capture_session_id,
            AudioChunkDocument.captured_at < ended_at,
            AudioChunkDocument.deleted == False,  # noqa: E712 - Beanie expression
        )
        .sort("+sequence")
        .to_list()
    )
    quarantine = AudioChunkDocument.get_pymongo_collection().database[
        "capture_cutover_quarantine"
    ]
    excluded_rows = await quarantine.find(
        {
            "source_chunk_id": {"$in": [str(chunk.id) for chunk in candidates]},
            "reason": "overlapping_operational_index",
        },
        {"source_chunk_id": 1},
    ).to_list(length=None)
    excluded_ids = {row["source_chunk_id"] for row in excluded_rows}
    candidates = [chunk for chunk in candidates if str(chunk.id) not in excluded_ids]
    chunks = [chunk for chunk in candidates if _chunk_end(chunk) > started_at]
    if not chunks:
        raise AudioClaimError(
            f"Capture session {capture_session_id} has no audio in requested window"
        )

    return _build_capture_ranges(
        capture_session_id=capture_session_id,
        capture_source_id=session.capture_source_id,
        time_basis=session.time_basis,
        chunks=chunks,
        started_at=started_at,
        ended_at=ended_at,
    )


async def claim_entire_capture(capture_session_id: str) -> list[AudioRangeRef]:
    chunks = (
        await AudioChunkDocument.find(
            AudioChunkDocument.capture_session_id == capture_session_id,
            AudioChunkDocument.deleted == False,  # noqa: E712 - Beanie expression
        )
        .sort("+sequence")
        .to_list()
    )
    if not chunks:
        raise AudioClaimError(f"Capture session {capture_session_id} has no audio")
    return await claim_capture_window(
        capture_session_id,
        as_utc(chunks[0].captured_at),
        max(_chunk_end(chunk) for chunk in chunks),
    )


async def apply_audio_ranges(
    conversation: Conversation,
    ranges: Sequence[AudioRangeRef],
    *,
    save: bool = True,
) -> Conversation:
    """Install claims and synchronize the Conversation's cheap audio summary."""
    if not ranges:
        raise AudioClaimError("conversation must claim at least one audio range")
    resolved = await resolve_audio_ranges(ranges)
    unique_chunks_by_id = {str(item.chunk.id): item.chunk for item in resolved}
    unique_chunks = list(unique_chunks_by_id.values())
    original = sum(chunk.original_size for chunk in unique_chunks)
    compressed = sum(chunk.compressed_size for chunk in unique_chunks)
    conversation.audio_ranges = list(ranges)
    conversation.started_at = min(as_utc(item.started_at) for item in ranges)
    conversation.ended_at = max(as_utc(item.ended_at) for item in ranges)
    conversation.created_at = conversation.started_at
    conversation.audio_chunks_count = len(unique_chunks)
    conversation.audio_total_duration = round(range_duration(ranges), 3)
    conversation.audio_compression_ratio = compressed / original if original else None
    if save:
        await conversation.save()
    return conversation


async def get_conversation_audio_ranges(conversation_id: str) -> list[AudioRangeRef]:
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id,
        Conversation.deleted == False,  # noqa: E712 - Beanie expression
    )
    if conversation is None:
        raise AudioClaimError(f"Conversation {conversation_id} not found")
    if not conversation.audio_ranges:
        raise AudioClaimError(f"Conversation {conversation_id} has no audio claim")
    return conversation.audio_ranges


async def resolve_conversation_audio(conversation_id: str) -> list[ClaimedChunk]:
    return await resolve_audio_ranges(
        await get_conversation_audio_ranges(conversation_id)
    )


async def locate_conversation_audio_at(
    when: datetime, *, chunk_lookback: timedelta = timedelta(minutes=5)
) -> Optional[ConversationAudioLocation]:
    """Find a live Conversation claim that can play an absolute capture instant.

    Capture chunks deliberately have no Conversation owner. This is the inverse claim
    lookup for tools that start from wall-clock time: locate a physical chunk, then a
    semantic claim containing that chunk, and finally map absolute time into the
    Conversation's gap-elided presentation timeline.
    """
    moment = as_utc(when)
    chunks = (
        await AudioChunkDocument.find(
            {
                "captured_at": {
                    "$lte": moment,
                    "$gte": moment - chunk_lookback,
                },
                "deleted": {"$ne": True},
            }
        )
        .sort("-captured_at")
        .to_list()
    )
    for chunk in chunks:
        chunk_start = as_utc(chunk.captured_at)
        if not (chunk_start <= moment < _chunk_end(chunk)):
            continue
        chunk_id = str(chunk.id)
        conversations = await Conversation.find(
            {
                "deleted": {"$ne": True},
                "audio_ranges.chunk_ids": chunk_id,
            }
        ).to_list()
        for conversation in sorted(
            conversations, key=lambda item: item.conversation_id
        ):
            presentation_cursor = 0.0
            for audio_range in conversation.audio_ranges:
                range_start = as_utc(audio_range.started_at)
                range_end = as_utc(audio_range.ended_at)
                if (
                    chunk_id in audio_range.chunk_ids
                    and range_start <= moment < range_end
                ):
                    return ConversationAudioLocation(
                        conversation_id=conversation.conversation_id,
                        offset_seconds=(
                            presentation_cursor + (moment - range_start).total_seconds()
                        ),
                        chunk_id=chunk_id,
                    )
                presentation_cursor += audio_range.duration_seconds
    return None


async def clip_audio_ranges(
    ranges: Sequence[AudioRangeRef], start_seconds: float, end_seconds: float
) -> list[AudioRangeRef]:
    """Clip claims using the gap-elided presentation timeline of a Conversation."""
    total = range_duration(ranges)
    start = max(0.0, float(start_seconds))
    end = min(float(end_seconds), total)
    if end <= start:
        raise AudioClaimError("audio range clip must have positive duration")

    chunks = await load_chunks_by_id(ordered_chunk_ids(ranges))
    chunks_by_id = {str(chunk.id): chunk for chunk in chunks}
    output: list[AudioRangeRef] = []
    cursor = 0.0
    for audio_range in ranges:
        next_cursor = cursor + audio_range.duration_seconds
        local_start = max(0.0, start - cursor)
        local_end = min(audio_range.duration_seconds, end - cursor)
        if local_end > local_start:
            absolute_start = as_utc(audio_range.started_at) + timedelta(
                seconds=local_start
            )
            absolute_end = as_utc(audio_range.started_at) + timedelta(seconds=local_end)
            chunk_ids = [
                chunk_id
                for chunk_id in audio_range.chunk_ids
                if as_utc(chunks_by_id[chunk_id].captured_at) < absolute_end
                and _chunk_end(chunks_by_id[chunk_id]) > absolute_start
            ]
            if chunk_ids:
                output.append(
                    audio_range.model_copy(
                        update={
                            "range_id": str(uuid.uuid4()),
                            "chunk_ids": chunk_ids,
                            "started_at": absolute_start,
                            "ended_at": absolute_end,
                        }
                    )
                )
        cursor = next_cursor
        if cursor >= end:
            break
    return output


async def partition_audio_ranges(
    ranges: Sequence[AudioRangeRef], split_points: Iterable[float]
) -> list[list[AudioRangeRef]]:
    points = sorted(set(float(point) for point in split_points))
    total = range_duration(ranges)
    if any(point <= 0 or point >= total for point in points):
        raise AudioClaimError(f"split points must lie inside (0, {total})")
    edges = [0.0, *points, total]
    return [
        await clip_audio_ranges(ranges, start, end)
        for start, end in zip(edges[:-1], edges[1:])
    ]


def merge_audio_ranges(
    groups: Iterable[Sequence[AudioRangeRef]],
) -> list[AudioRangeRef]:
    """Concatenate claims without moving or rewriting their chunks."""
    return [audio_range for group in groups for audio_range in group]


async def chunk_ids_still_referenced(chunk_ids: Sequence[str]) -> set[str]:
    """Return the subset still claimed by any live semantic/evidence document."""
    if not chunk_ids:
        return set()
    requested = set(chunk_ids)
    referenced: set[str] = set()
    claimant_queries = [
        (
            Conversation.get_pymongo_collection(),
            {
                "deleted": {"$ne": True},
                "audio_ranges.chunk_ids": {"$in": list(chunk_ids)},
            },
            "audio_ranges",
        ),
        (
            TimelineEpisode.get_pymongo_collection(),
            {
                "deleted": {"$ne": True},
                "audio_ranges.chunk_ids": {"$in": list(chunk_ids)},
            },
            "audio_ranges",
        ),
        (
            TranscriptArtifact.get_pymongo_collection(),
            {"audio_ranges.chunk_ids": {"$in": list(chunk_ids)}},
            "audio_ranges",
        ),
        (
            DiarizationArtifact.get_pymongo_collection(),
            {"audio_ranges.chunk_ids": {"$in": list(chunk_ids)}},
            "audio_ranges",
        ),
        (
            AudioEvidenceSpan.get_pymongo_collection(),
            {"audio_ranges.chunk_ids": {"$in": list(chunk_ids)}},
            "audio_ranges",
        ),
    ]
    for collection, query, field in claimant_queries:
        rows = await collection.find(query, {f"{field}.chunk_ids": 1}).to_list(
            length=None
        )
        for row in rows:
            values = row.get(field, [])
            ranges = values if isinstance(values, list) else [values]
            for audio_range in ranges:
                referenced.update(
                    chunk_id
                    for chunk_id in audio_range.get("chunk_ids", [])
                    if chunk_id in requested
                )
    return referenced
