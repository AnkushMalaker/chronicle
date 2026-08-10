"""Validate provider timestamps before they become an active transcript.

Paid transcription responses are cached before this boundary.  This module therefore
normalizes copies for operational storage while leaving the exact provider response in
the content-hash cache for inspection and reuse.
"""

import copy
import math
from typing import Any

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument

TIMING_EDGE_TOLERANCE_SECONDS = 1.0


class TranscriptTimingError(ValueError):
    """A provider transcript cannot describe the conversation's current audio."""

    def __init__(self, code: str, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.code = code
        self.details = details


def _number(value: Any, *, field: str, kind: str, index: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TranscriptTimingError(
            "transcript_timing_invalid_value",
            f"{kind} {index} has a non-numeric {field} timestamp",
            {"kind": kind, "index": index, "field": field, "value": value},
        ) from exc
    if not math.isfinite(number):
        raise TranscriptTimingError(
            "transcript_timing_invalid_value",
            f"{kind} {index} has a non-finite {field} timestamp",
            {"kind": kind, "index": index, "field": field, "value": value},
        )
    return number


def validate_and_normalize_transcript_timing(
    segments: list[dict],
    words: list[dict],
    *,
    audio_duration: float,
    audio_ranges: list[tuple[float, float]] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return clean copies, clipping only harmless provider edge overhangs.

    A timestamp that begins beyond the audio or exceeds it by more than one second is
    not repaired heuristically: it normally means a pre-split/pre-trim clock was copied
    onto different audio.  The caller must quarantine that result and retranscribe the
    current content instead.
    """

    duration = float(audio_duration or 0.0)
    if not math.isfinite(duration) or duration <= 0:
        raise TranscriptTimingError(
            "transcript_audio_missing",
            "Cannot attach a timed transcript to a conversation without audio",
            {"audio_duration": duration},
        )

    clean_segments = copy.deepcopy(segments or [])
    clean_words = copy.deepcopy(words or [])
    max_timing = 0.0

    normalized: dict[str, list[dict]] = {"segment": [], "word": []}
    for kind, items in (("segment", clean_segments), ("word", clean_words)):
        for index, item in enumerate(items):
            start = _number(
                item.get("start", 0.0), field="start", kind=kind, index=index
            )
            end = _number(item.get("end", 0.0), field="end", kind=kind, index=index)
            max_timing = max(max_timing, start, end)
            if start < 0 or end < start:
                raise TranscriptTimingError(
                    "transcript_timing_invalid_range",
                    f"{kind} {index} has invalid range {start:.3f}s-{end:.3f}s",
                    {
                        "kind": kind,
                        "index": index,
                        "start": start,
                        "end": end,
                        "audio_duration": duration,
                        "max_timing": max_timing,
                    },
                )
            if start >= duration:
                if end <= duration + TIMING_EDGE_TOLERANCE_SECONDS:
                    # A provider can emit a final token a few milliseconds after the
                    # decoded endpoint. It has no audio overlap, so retaining it would
                    # create an unreconstructable range; the cached raw result still
                    # preserves it for audit.
                    continue
                raise TranscriptTimingError(
                    "transcript_timing_out_of_bounds",
                    f"{kind} {index} range {start:.3f}s-{end:.3f}s exceeds "
                    f"{duration:.3f}s audio",
                    {
                        "kind": kind,
                        "index": index,
                        "start": start,
                        "end": end,
                        "audio_duration": duration,
                        "max_timing": max_timing,
                    },
                )
            if end > duration + TIMING_EDGE_TOLERANCE_SECONDS:
                raise TranscriptTimingError(
                    "transcript_timing_out_of_bounds",
                    f"{kind} {index} range {start:.3f}s-{end:.3f}s exceeds "
                    f"{duration:.3f}s audio",
                    {
                        "kind": kind,
                        "index": index,
                        "start": start,
                        "end": end,
                        "audio_duration": duration,
                        "max_timing": max_timing,
                    },
                )
            item["start"] = start
            item["end"] = min(end, duration)
            normalized[kind].append(item)

    clean_segments = normalized["segment"]
    clean_words = normalized["word"]
    if audio_ranges is not None:
        coverage_items = clean_segments or clean_words
        for index, item in enumerate(coverage_items):
            start = float(item["start"])
            end = float(item["end"])
            if end <= start:
                continue
            if not any(
                range_start < end and range_end > start
                for range_start, range_end in audio_ranges
            ):
                raise TranscriptTimingError(
                    "transcript_audio_gap",
                    f"timed item {index} range {start:.3f}s-{end:.3f}s "
                    "has no backing audio chunk",
                    {
                        "index": index,
                        "start": start,
                        "end": end,
                        "audio_duration": duration,
                        "audio_range_count": len(audio_ranges),
                    },
                )

    return clean_segments, clean_words


async def load_transcript_audio_ranges(
    conversation_id: str,
) -> list[tuple[float, float]]:
    """Load current non-deleted chunk coverage for ingest/preflight checks."""

    chunks = await AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conversation_id,
        AudioChunkDocument.deleted == False,  # noqa: E712 - Beanie needs ==
    ).to_list()
    return [(float(chunk.start_time), float(chunk.end_time)) for chunk in chunks]
