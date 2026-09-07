"""Clip-plan computation for annotation exports.

One code path decides what an annotation export will contain — which speech
regions become clips, at which boundaries, with which transcript slices —
shared by the export RQ job (``workers/data_audit_jobs.py``, which renders the
plan to WAVs in a zip) and the synchronous preview endpoint (which returns the
plan for the user to verify and curate before anything is written). Keeping
both on the same functions means the preview can never drift from the export.

Two kinds of range carving, deliberately accounted separately:

- ``excluded_ranges`` — privacy-screen withholdings; reported as
  ``excluded_seconds`` ("withheld").
- ``dropped_ranges`` — clips the user unticked in the export preview; reported
  as ``dropped_seconds``. Same subtraction mechanics, different meaning: one is
  "too sensitive to share", the other is "not worth annotating".
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from backend.models.conversation import Conversation
from backend.services.audio_claims import resolve_conversation_audio
from backend.utils.vad_analysis import (
    frame_speech_intervals,
    merge_speech_regions,
    subtract_intervals,
)

logger = logging.getLogger(__name__)


@dataclass
class ClipPlan:
    """One planned clip: a speech region and its position in the source."""

    clip_index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class ConversationPlan:
    """The export plan for one conversation (or the reason it has none)."""

    conversation: Optional[Conversation]
    clips: List[ClipPlan] = field(default_factory=list)
    sample_rate: int = 16000
    excluded_seconds: float = 0.0  # privacy-screen withholdings
    dropped_seconds: float = 0.0  # preview-dropped clips
    skipped_reason: Optional[str] = None

    @property
    def clip_seconds(self) -> float:
        return sum(c.duration for c in self.clips)


def export_eligibility(
    conv: Optional[Conversation], user_id: str, is_superuser: bool
) -> Optional[str]:
    """Why this conversation cannot be exported, or None if it can."""
    if not conv:
        return "not found"
    if not is_superuser and conv.user_id != user_id:
        return "access forbidden"
    if conv.deleted:
        return "deleted"
    if conv.audio_archived:
        return "audio archived"
    if not conv.audio_chunks_count:
        return "no audio"
    return None


async def collect_raw_intervals(
    conversation_id: str, threshold: float
) -> Tuple[Optional[List[List[float]]], float, int]:
    """Raw speech intervals from cached chunk frame scores (streaming cursor).

    Returns (intervals, last_chunk_end_seconds, sample_rate); intervals is
    None when any chunk lacks VAD scores (caller should analyze first).
    """
    claimed = await resolve_conversation_audio(conversation_id)
    intervals: List[List[float]] = []
    last_end = 0.0
    sample_rate = 16000
    first = True
    for item in claimed:
        chunk = item.chunk
        if first:
            sample_rate = int(chunk.sample_rate or 16000)
            first = False
        vad = chunk.vad
        if vad is None or vad.scores is None:
            return None, 0.0, sample_rate
        base = item.conversation_start_seconds - item.clip_start_seconds
        item_intervals = frame_speech_intervals(
            vad.scores,
            float(vad.frame_hop_ms) / 1000.0,
            base,
            threshold=threshold,
        )
        claim_start = item.conversation_start_seconds
        claim_end = claim_start + item.duration_seconds
        intervals.extend(
            [max(start, claim_start), min(end, claim_end)]
            for start, end in item_intervals
            if start < claim_end and end > claim_start
        )
        last_end = claim_end
    return intervals, last_end, sample_rate


async def plan_conversation_clips(
    conv: Conversation,
    mode: str,
    pad_seconds: float,
    speech_threshold: float,
    merge_gap_seconds: float,
    excluded_ranges: Optional[List[List[float]]] = None,
    dropped_ranges: Optional[List[List[float]]] = None,
) -> ConversationPlan:
    """Compute the clip plan for one eligible conversation.

    Mode ``clips``: one region per VAD speech run, padded and gap-merged at
    the requested settings (the cached ``speech_regions`` use the default
    0.3s pad, so regions are always re-merged here). Mode ``full``: a single
    region spanning the whole recording.

    ``excluded_ranges`` (privacy screen) and ``dropped_ranges`` (preview
    curation) are subtracted **after** padding/merge so padding cannot
    re-expose a cut. A dropped clip's exact [start, end] therefore removes
    precisely that region.

    Unanalyzed audio yields ``skipped_reason='not analyzed'`` — the caller
    decides whether to run VAD (the export job does; the synchronous preview
    endpoint does not, pointing the user at the Analyze button instead).
    """
    cid = conv.conversation_id

    if mode == "full":
        duration = conv.audio_total_duration or 0.0
        if duration <= 0:
            return ConversationPlan(conv, skipped_reason="no audio duration")
        regions: List[List[float]] = [[0.0, duration]]
        claimed = await resolve_conversation_audio(cid)
        sample_rate = claimed[0].chunk.sample_rate if claimed else 16000
    else:
        intervals, last_end, sample_rate = await collect_raw_intervals(
            cid, speech_threshold
        )
        if intervals is None:
            return ConversationPlan(
                conv, sample_rate=sample_rate, skipped_reason="not analyzed"
            )
        duration = conv.audio_total_duration or last_end
        regions = merge_speech_regions(
            intervals,
            duration,
            pad_seconds=pad_seconds,
            merge_gap_seconds=merge_gap_seconds,
        )

    kept = sum(t1 - t0 for t0, t1 in regions)
    if excluded_ranges:
        regions = subtract_intervals(regions, excluded_ranges)
    after_privacy = sum(t1 - t0 for t0, t1 in regions)
    if dropped_ranges:
        regions = subtract_intervals(regions, dropped_ranges)
    after_drop = sum(t1 - t0 for t0, t1 in regions)

    return ConversationPlan(
        conv,
        clips=[ClipPlan(i, t0, t1) for i, (t0, t1) in enumerate(regions)],
        sample_rate=sample_rate,
        excluded_seconds=round(max(0.0, kept - after_privacy), 2),
        dropped_seconds=round(max(0.0, after_privacy - after_drop), 2),
    )
