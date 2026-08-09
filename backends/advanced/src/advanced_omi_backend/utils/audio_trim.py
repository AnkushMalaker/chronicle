"""Plan and re-time a silence trim over a conversation's audio chunks.

A continuous-capture recording is mostly silence: measured over this deployment's
ScreenPipe corpus, 95.9 hours of stored audio carried 24.9 hours of speech. Keeping
the silence inside the conversation makes it unplayable and unreadable, but deleting
it throws away evidence that the capture existed at all.

So a trim *moves* chunks rather than dropping them. Chunks with no speech are handed
to a soft-deleted remnant conversation, the surviving chunks are renumbered to be
contiguous again, and the transcript is re-timed through the same map. Every chunk
keeps its immutable ``captured_at``, so a remnant needs no "trimmed from here" note:
the audio says when it happened, which is the only provenance that survives further
splits and merges.

This module is pure. It decides *what* to move and how time maps; applying it belongs
to the caller that owns the documents.
"""

from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

from advanced_omi_backend.models.conversation import Conversation

# (old_start, old_end, new_start) for one surviving stretch of conversation time.
KeptRegion = Tuple[float, float, float]


@dataclass
class TrimPlan:
    """Chunk moves and the time map they imply.

    ``keep``/``drop`` are chunk indices in the pre-trim numbering. ``regions`` maps
    surviving pre-trim time onto post-trim time and is what the transcript is
    re-timed through.
    """

    keep: List[int]
    drop: List[int]
    regions: List[KeptRegion] = field(default_factory=list)
    kept_seconds: float = 0.0
    dropped_seconds: float = 0.0

    @property
    def trims(self) -> bool:
        return bool(self.drop)


def _covered(
    intervals: Sequence[Tuple[float, float]], start: float, end: float
) -> float:
    """Seconds of [start, end) covered by ``intervals`` (assumed sorted, disjoint)."""
    total = 0.0
    for a, b in intervals:
        if b <= start:
            continue
        if a >= end:
            break
        total += min(b, end) - max(a, start)
    return total


def plan_silence_trim(
    chunks: Sequence[dict],
    speech_intervals: Sequence[Tuple[float, float]],
    *,
    pad_seconds: float = 5.0,
    min_run_seconds: float = 120.0,
    min_saving_seconds: float = 60.0,
) -> TrimPlan:
    """Choose which chunks to move out of a conversation.

    A chunk is kept when it overlaps speech, or padding around speech. Chunks are the
    unit of decision because they are the unit of storage — cutting inside one would
    mean re-encoding audio, which is exactly the cost this design avoids.

    Only *runs* of droppable chunks at least ``min_run_seconds`` long are actually
    dropped. Without that a natural pause in a conversation becomes a cut, and playback
    starts skipping. The threshold is deliberately far longer than any conversational
    pause: it is there to find the stretches where nothing was happening at all.

    Args:
        chunks: dicts with ``chunk_index``/``start_time``/``end_time``/``duration``,
            sorted by ``chunk_index``.
        speech_intervals: (start, end) seconds of speech in conversation time.
        pad_seconds: context retained on each side of a speech interval.
        min_run_seconds: shortest silent run worth cutting.
        min_saving_seconds: leave the conversation alone below this total saving.
    """
    keep_all = TrimPlan(
        keep=[int(c["chunk_index"]) for c in chunks],
        drop=[],
        regions=[],
        kept_seconds=sum(float(c.get("duration") or 0.0) for c in chunks),
        dropped_seconds=0.0,
    )
    if not chunks:
        return TrimPlan(keep=[], drop=[])
    if not speech_intervals:
        # Every chunk is droppable, so the arithmetic below would empty the
        # conversation. Whether audio with no speech should exist at all is the
        # speech gate's decision; this function only ever removes silence from
        # around speech it was told about.
        return keep_all

    padded = sorted(
        (max(0.0, start - pad_seconds), end + pad_seconds)
        for start, end in speech_intervals
        if end > start
    )
    merged: List[Tuple[float, float]] = []
    for start, end in padded:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # A chunk survives if any of its audio is inside the padded speech.
    keeps = [
        _covered(merged, float(c["start_time"]), float(c["end_time"])) > 0.0
        for c in chunks
    ]

    # Re-keep short silent runs: only long ones are worth a cut.
    index = 0
    while index < len(chunks):
        if keeps[index]:
            index += 1
            continue
        run_end = index
        while run_end < len(chunks) and not keeps[run_end]:
            run_end += 1
        run_seconds = float(chunks[run_end - 1]["end_time"]) - float(
            chunks[index]["start_time"]
        )
        if run_seconds < min_run_seconds:
            for position in range(index, run_end):
                keeps[position] = True
        index = run_end

    keep = [int(c["chunk_index"]) for c, alive in zip(chunks, keeps) if alive]
    drop = [int(c["chunk_index"]) for c, alive in zip(chunks, keeps) if not alive]
    dropped_seconds = sum(
        float(c.get("duration") or 0.0) for c, alive in zip(chunks, keeps) if not alive
    )
    if not drop or dropped_seconds < min_saving_seconds:
        return keep_all

    # Time map: each surviving run of chunks becomes one region, packed against the
    # previous one. Region ends use the last kept chunk's end_time, so a partial final
    # chunk keeps its true length.
    regions: List[KeptRegion] = []
    cursor = 0.0
    index = 0
    while index < len(chunks):
        if not keeps[index]:
            index += 1
            continue
        run_start = index
        while index < len(chunks) and keeps[index]:
            index += 1
        old_start = float(chunks[run_start]["start_time"])
        old_end = float(chunks[index - 1]["end_time"])
        regions.append((old_start, old_end, cursor))
        cursor += old_end - old_start

    return TrimPlan(
        keep=keep,
        drop=drop,
        regions=regions,
        kept_seconds=cursor,
        dropped_seconds=dropped_seconds,
    )


def _shift_for(time: float, regions: Sequence[KeptRegion]) -> float | None:
    """Offset mapping pre-trim ``time`` into post-trim time, or None if it was cut."""
    for old_start, old_end, new_start in regions:
        if old_start <= time < old_end:
            return new_start - old_start
    return None


def remap_words(
    words: List[Conversation.Word], regions: Sequence[KeptRegion]
) -> List[Conversation.Word]:
    """Re-time words onto the trimmed timeline, dropping any that were cut."""
    remapped = []
    for word in words:
        shift = _shift_for(word.start, regions)
        if shift is None:
            continue
        remapped.append(
            word.model_copy(
                update={
                    "start": round(word.start + shift, 3),
                    "end": round(word.end + shift, 3),
                }
            )
        )
    return remapped


def remap_segments(
    segments: List[Conversation.SpeakerSegment], regions: Sequence[KeptRegion]
) -> List[Conversation.SpeakerSegment]:
    """Re-time segments onto the trimmed timeline, dropping any that were cut.

    Membership uses the segment midpoint, matching split/merge (``transcript_slicing``).
    Cuts land in padded silence by construction, so a segment cannot straddle one; the
    midpoint rule decides the degenerate cases without producing torn segments.
    """
    remapped = []
    for segment in segments:
        shift = _shift_for((segment.start + segment.end) / 2, regions)
        if shift is None:
            continue
        remapped.append(
            segment.model_copy(
                update={
                    "start": round(segment.start + shift, 3),
                    "end": round(segment.end + shift, 3),
                    "words": remap_words(segment.words or [], regions),
                }
            )
        )
    return remapped
