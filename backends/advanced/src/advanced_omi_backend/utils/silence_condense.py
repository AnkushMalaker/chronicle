"""Silence-aware condensing for paid batch transcription.

Batch STT providers bill by audio duration — silence included. Long-form
recordings (wearables, meeting rooms) are often mostly silence, so before
sending audio to the provider we run the local TEN VAD over the PCM, cut out
silences longer than ``CUT_GAP_SECONDS`` (keeping ``PAD_SECONDS`` of context
around speech), and transcribe the condensed audio instead. Word and segment
timestamps are then mapped back onto the original timeline, splitting any
segment that spans a cut, so everything downstream (speaker identification,
enrollment clips, playback) still refers to real conversation time.

Condensing is skipped when it wouldn't save at least ``MIN_SAVINGS_FRACTION``
of the audio, and produces an empty result without any provider call when the
audio contains no speech at all.
"""

import logging
from typing import List, Optional, Tuple

from advanced_omi_backend.utils.vad_analysis import (
    frame_speech_intervals,
    merge_speech_regions,
    score_pcm_frames,
)

logger = logging.getLogger(__name__)

MIN_AUDIO_SECONDS = 10.0  # below this, condensing overhead isn't worth it
MIN_SAVINGS_FRACTION = 0.15  # send original unless we cut at least this much
CUT_GAP_SECONDS = 3.0  # only cut silences longer than this
PAD_SECONDS = 0.5  # context kept around each speech region

# (condensed_start_seconds, original_start_seconds, length_seconds) per region
CondenseMap = List[Tuple[float, float, float]]


def condense_silence(
    pcm_data: bytes, sample_rate: int, channels: int, sample_width: int
) -> Tuple[bytes, Optional[CondenseMap], Optional[float]]:
    """Cut long silences out of PCM audio using the local VAD.

    Returns ``(pcm, mapping, speech_seconds)``:
    - ``mapping is None`` — audio unchanged (too short, wrong format, VAD
      unavailable, or not enough silence to be worth cutting).
    - ``mapping == []`` — no speech at all; ``pcm`` is empty and the caller
      should skip transcription entirely.
    - otherwise — ``pcm`` is the condensed audio and ``mapping`` translates
      condensed time back to original time (see :func:`remap_condensed_result`).
    """
    if sample_width != 2:
        return pcm_data, None, None
    frame_bytes = sample_width * channels
    total_samples = len(pcm_data) // frame_bytes
    if not sample_rate or total_samples <= 0:
        return pcm_data, None, None
    duration = total_samples / sample_rate
    if duration < MIN_AUDIO_SECONDS:
        return pcm_data, None, None

    try:
        scores, hop_seconds = score_pcm_frames(pcm_data, sample_rate, channels)
    except Exception as e:
        logger.warning("Silence condensing skipped (VAD failed): %s", e)
        return pcm_data, None, None

    raw_intervals = frame_speech_intervals(scores, hop_seconds, 0.0)
    regions = merge_speech_regions(
        raw_intervals,
        duration,
        pad_seconds=PAD_SECONDS,
        merge_gap_seconds=CUT_GAP_SECONDS,
        max_count=100_000,  # never coarsen: every region maps to billed audio
    )
    if not regions:
        return b"", [], 0.0

    speech_seconds = sum(end - start for start, end in regions)
    if speech_seconds >= duration * (1.0 - MIN_SAVINGS_FRACTION):
        return pcm_data, None, speech_seconds

    pieces: List[bytes] = []
    mapping: CondenseMap = []
    cursor = 0.0
    for start, end in regions:
        byte_start = int(start * sample_rate) * frame_bytes
        byte_end = min(int(end * sample_rate) * frame_bytes, len(pcm_data))
        piece = pcm_data[byte_start:byte_end]
        if not piece:
            continue
        length = len(piece) / (sample_rate * frame_bytes)
        pieces.append(piece)
        mapping.append((cursor, start, length))
        cursor += length

    if not mapping:
        return b"", [], 0.0
    logger.info(
        "🔇 Condensed %.0fs audio to %.0fs of speech (%d regions, %.0f%% saved)",
        duration,
        cursor,
        len(mapping),
        (1.0 - cursor / duration) * 100,
    )
    return b"".join(pieces), mapping, speech_seconds


def _region_index(t: float, mapping: CondenseMap) -> int:
    """Region containing condensed time ``t``; exact boundaries belong to the
    NEXT region (a time at a cut is the start of the following speech)."""
    for index, (cond_start, _orig, length) in enumerate(mapping):
        if t < cond_start + length - 1e-6:
            return index
    return len(mapping) - 1


def _to_original_in(t: float, region: Tuple[float, float, float]) -> float:
    cond_start, orig_start, length = region
    return orig_start + max(0.0, min(t - cond_start, length))


def _word_text(word: dict) -> str:
    return str(
        word.get("punctuated_word") or word.get("word") or word.get("text") or ""
    ).strip()


def remap_condensed_result(result: dict, mapping: CondenseMap) -> dict:
    """Rewrite a transcription result from condensed time to original time.

    Words are pointlike and shift directly. A segment that spans a cut is
    split into one sub-segment per speech region — text is redistributed by
    word timestamps when the provider returned words, otherwise the full text
    stays on the piece with the largest share of the segment.
    """
    if not mapping:
        return result

    words = result.get("words") or []
    # Group words by region while still in condensed time.
    word_regions = [_region_index(w.get("start", 0.0), mapping) for w in words]

    segments = result.get("segments") or []
    new_segments = []
    for segment in segments:
        seg_start = float(segment.get("start", 0.0))
        seg_end = float(segment.get("end", seg_start))
        first = _region_index(seg_start, mapping)
        last = _region_index(max(seg_end - 1e-3, seg_start), mapping)
        if first == last:
            new_segments.append(
                {
                    **segment,
                    "start": round(_to_original_in(seg_start, mapping[first]), 3),
                    "end": round(_to_original_in(seg_end, mapping[first]), 3),
                }
            )
            continue

        # Segment spans a cut: one sub-segment per region it touches.
        seg_words = [
            (w, r)
            for w, r in zip(words, word_regions)
            if seg_start - 1e-3 <= w.get("start", 0.0) < seg_end + 1e-3
        ]
        pieces = []
        for region_index in range(first, last + 1):
            region = mapping[region_index]
            cond_start, _orig, length = region
            piece_start = max(seg_start, cond_start)
            piece_end = min(seg_end, cond_start + length)
            if piece_end <= piece_start:
                continue
            in_piece = [w for w, r in seg_words if r == region_index]
            text = " ".join(filter(None, (_word_text(w) for w in in_piece)))
            pieces.append(
                {
                    **segment,
                    "start": round(_to_original_in(piece_start, region), 3),
                    "end": round(_to_original_in(piece_end, region), 3),
                    "text": text,
                    "_overlap": piece_end - piece_start,
                }
            )
        if pieces and not any(p["text"] for p in pieces):
            # No word timestamps: keep the full text on the largest piece.
            largest = max(pieces, key=lambda p: p["_overlap"])
            largest["text"] = segment.get("text", "")
        for piece in pieces:
            piece.pop("_overlap", None)
        new_segments.extend(pieces)

    # Words are pointlike: map both ends through the region their start is in,
    # so a word never straddles a cut.
    for word, region_index in zip(words, word_regions):
        region = mapping[region_index]
        start = word.get("start", 0.0)
        end = word.get("end", start)
        word["start"] = round(_to_original_in(start, region), 3)
        word["end"] = round(_to_original_in(end, region), 3)

    result["segments"] = new_segments
    return result
