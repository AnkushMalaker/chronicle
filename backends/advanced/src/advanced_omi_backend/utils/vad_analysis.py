"""VAD speech analysis for conversation audio.

Decodes the Opus chunks stored in MongoDB and scores them with the configured
VAD provider (see ``services/vad``). Frame-level speech probabilities are
written to each chunk document as a self-contained ``VADResult`` subdocument
(``vad``); a threshold-independent probability histogram is returned for
caching on the conversation (``Conversation.VadAnalysis``) so callers can
derive the speech fraction for any threshold without re-decoding.

Used by the Data Audit feature to surface speech-free conversations for
archival and to locate long silence gaps for splitting.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
from pymongo import UpdateOne

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.services.vad import get_vad_provider
from advanced_omi_backend.utils.audio_chunk_utils import (
    concatenate_chunks_to_pcm,
    retrieve_audio_chunks,
)

logger = logging.getLogger(__name__)

# Analysis parameters
BATCH_CHUNKS = 30  # decode 30 x 10s chunks (~5 min) at a time to bound memory
HISTOGRAM_BINS = 20  # probability bins over [0, 1]
HISTOGRAM_BIN_WIDTH = 1.0 / HISTOGRAM_BINS
SPEECH_PROB_THRESHOLD = 0.5  # default frame prob at/above which audio counts as speech
EVIDENCE_BUCKET_SECONDS = 10.0
ENERGY_FRAME_SECONDS = 0.1
ACOUSTIC_ACTIVE_DBFS = -45.0

# Speech-region derivation parameters
REGION_PAD_SECONDS = 0.3  # widen each region so playback doesn't clip word edges
REGION_MERGE_GAP_SECONDS = 3.0  # regions closer than this become one
REGION_MIN_SECONDS = 0.4  # drop isolated blips shorter than this
REGION_MAX_COUNT = 500  # cap stored regions (merge gap doubles until under cap)


def _pcm_to_mono_int16(pcm: bytes, channels: int) -> np.ndarray:
    samples = np.frombuffer(pcm, dtype=np.int16)
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1).astype(np.int16)
    return samples


VAD_SAMPLE_RATE = 16000


class SpeechDetectionReason(str, Enum):
    """Stable reason codes for speech-gate decisions and fail-open events."""

    SPEECH_DETECTED = "speech_detected"
    NO_SPEECH = "no_speech"
    EMPTY_AUDIO = "empty_audio"
    UNSUPPORTED_SAMPLE_WIDTH = "unsupported_sample_width"
    INVALID_CHANNELS = "invalid_channels"
    INVALID_SAMPLE_RATE = "invalid_sample_rate"
    PCM_PREPARATION_FAILED = "pcm_preparation_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"
    WAV_DECODE_FAILED = "wav_decode_failed"


@dataclass(frozen=True)
class SpeechDetectionResult:
    """A conclusive speech verdict or an explicit fail-open reason.

    ``has_speech=None`` means no trustworthy verdict was produced. Callers
    should use ``should_reject`` rather than interpreting the fields themselves.
    Empty audio is conclusively rejectable without running the VAD, so it has
    ``has_speech=False`` and ``scored=False``.
    """

    has_speech: Optional[bool]
    scored: bool
    reason: SpeechDetectionReason
    detail: Optional[str] = None

    @property
    def should_reject(self) -> bool:
        """Only definitive silence or empty audio may be rejected."""
        return self.has_speech is False

    @classmethod
    def speech(cls) -> "SpeechDetectionResult":
        return cls(True, True, SpeechDetectionReason.SPEECH_DETECTED)

    @classmethod
    def no_speech(cls) -> "SpeechDetectionResult":
        return cls(False, True, SpeechDetectionReason.NO_SPEECH)

    @classmethod
    def empty_audio(cls) -> "SpeechDetectionResult":
        return cls(False, False, SpeechDetectionReason.EMPTY_AUDIO)

    @classmethod
    def unscored(
        cls, reason: SpeechDetectionReason, detail: str
    ) -> "SpeechDetectionResult":
        return cls(None, False, reason, detail)


@dataclass(frozen=True)
class VadFrameScores:
    """Frame-level VAD output with its time base."""

    scores: np.ndarray
    hop_seconds: float
    provider: Optional[str] = None


@dataclass(frozen=True)
class AudioEvidenceProfile:
    """Compact, sliceable signal profile for continuous-capture audio."""

    scored: bool
    reason: SpeechDetectionReason
    bucket_seconds: float
    speech_seconds: Optional[float]
    longest_no_speech_seconds: Optional[float]
    acoustic_active_seconds: float
    acoustic_quiet_seconds: float
    speech_fraction: list[Optional[float]]
    acoustic_active_fraction: list[float]
    rms_dbfs: list[Optional[float]]
    peak_dbfs: list[Optional[float]]
    provider: Optional[str]
    frame_hop_ms: Optional[float]


class VadScoringError(RuntimeError):
    """VAD scoring failure with a stable observability reason."""

    def __init__(self, reason: SpeechDetectionReason, detail: str):
        super().__init__(detail)
        self.reason = reason


def _exception_detail(error: Exception) -> str:
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def _log_unscored(result: SpeechDetectionResult) -> None:
    logger.warning(
        "speech_gate_unscored reason=%s detail=%s",
        result.reason.value,
        result.detail,
    )


def score_pcm_frames(
    pcm_data: bytes, sample_rate: int, channels: int
) -> VadFrameScores:
    """Run the configured VAD over raw 16-bit PCM.

    Audio is resampled to ``VAD_SAMPLE_RATE`` for scoring only. Raises a
    ``VadScoringError`` whose reason distinguishes PCM preparation, provider
    initialization, and provider execution failures.
    """
    try:
        mono = _pcm_to_mono_int16(pcm_data, channels)
        if sample_rate != VAD_SAMPLE_RATE:
            n16 = int(mono.size * VAD_SAMPLE_RATE / sample_rate)
            positions = np.linspace(0, mono.size - 1, n16)
            mono = np.interp(
                positions, np.arange(mono.size), mono.astype(np.float32)
            ).astype(np.int16)
    except Exception as error:
        raise VadScoringError(
            SpeechDetectionReason.PCM_PREPARATION_FAILED,
            _exception_detail(error),
        ) from error

    try:
        provider = get_vad_provider()
    except Exception as error:
        raise VadScoringError(
            SpeechDetectionReason.PROVIDER_UNAVAILABLE,
            _exception_detail(error),
        ) from error

    try:
        scores = provider.score(mono, VAD_SAMPLE_RATE)
    except Exception as error:
        raise VadScoringError(
            SpeechDetectionReason.PROVIDER_ERROR,
            _exception_detail(error),
        ) from error

    return VadFrameScores(
        scores=scores,
        hop_seconds=provider.frame_hop_ms / 1000.0,
        provider=provider.name,
    )


def _dbfs(value: float) -> Optional[float]:
    if value <= 0:
        return None
    return float(20.0 * np.log10(value / 32768.0))


def _longest_false_run(values: np.ndarray, seconds_per_value: float) -> float:
    longest = current = 0
    for value in values:
        if value:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest * seconds_per_value


def profile_pcm_audio(
    pcm_data: bytes,
    sample_rate: int,
    channels: int,
    sample_width: int,
    bucket_seconds: float = EVIDENCE_BUCKET_SECONDS,
) -> AudioEvidenceProfile:
    """Measure voice and general acoustic activity without conflating the two."""
    if sample_width != 2 or sample_rate <= 0 or channels <= 0:
        reason = (
            SpeechDetectionReason.UNSUPPORTED_SAMPLE_WIDTH
            if sample_width != 2
            else (
                SpeechDetectionReason.INVALID_SAMPLE_RATE
                if sample_rate <= 0
                else SpeechDetectionReason.INVALID_CHANNELS
            )
        )
        return AudioEvidenceProfile(
            scored=False,
            reason=reason,
            bucket_seconds=bucket_seconds,
            speech_seconds=None,
            longest_no_speech_seconds=None,
            acoustic_active_seconds=0,
            acoustic_quiet_seconds=0,
            speech_fraction=[],
            acoustic_active_fraction=[],
            rms_dbfs=[],
            peak_dbfs=[],
            provider=None,
            frame_hop_ms=None,
        )

    mono = _pcm_to_mono_int16(pcm_data, channels).astype(np.float32)
    duration = mono.size / sample_rate
    bucket_samples = max(1, int(round(bucket_seconds * sample_rate)))
    energy_samples = max(1, int(round(ENERGY_FRAME_SECONDS * sample_rate)))
    bucket_count = max(1, int(np.ceil(duration / bucket_seconds)))
    rms_series: list[Optional[float]] = []
    peak_series: list[Optional[float]] = []
    acoustic_series: list[float] = []
    active_seconds = 0.0

    for bucket_index in range(bucket_count):
        bucket = mono[
            bucket_index * bucket_samples : (bucket_index + 1) * bucket_samples
        ]
        if not bucket.size:
            rms_series.append(None)
            peak_series.append(None)
            acoustic_series.append(0.0)
            continue
        rms_series.append(_dbfs(float(np.sqrt(np.mean(np.square(bucket))))))
        peak_series.append(_dbfs(float(np.max(np.abs(bucket)))))
        active = measured = 0.0
        for offset in range(0, bucket.size, energy_samples):
            frame = bucket[offset : offset + energy_samples]
            frame_seconds = frame.size / sample_rate
            measured += frame_seconds
            frame_rms = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0
            if (_dbfs(frame_rms) or -120.0) >= ACOUSTIC_ACTIVE_DBFS:
                active += frame_seconds
        active_seconds += active
        acoustic_series.append(active / measured if measured else 0.0)

    try:
        vad = score_pcm_frames(pcm_data, sample_rate, channels)
    except VadScoringError as error:
        return AudioEvidenceProfile(
            scored=False,
            reason=error.reason,
            bucket_seconds=bucket_seconds,
            speech_seconds=None,
            longest_no_speech_seconds=None,
            acoustic_active_seconds=active_seconds,
            acoustic_quiet_seconds=max(0.0, duration - active_seconds),
            speech_fraction=[None] * bucket_count,
            acoustic_active_fraction=acoustic_series,
            rms_dbfs=rms_series,
            peak_dbfs=peak_series,
            provider=None,
            frame_hop_ms=None,
        )

    speech = vad.scores >= SPEECH_PROB_THRESHOLD
    speech_series: list[Optional[float]] = []
    for bucket_index in range(bucket_count):
        first = int(bucket_index * bucket_seconds / vad.hop_seconds)
        last = int((bucket_index + 1) * bucket_seconds / vad.hop_seconds)
        values = speech[first:last]
        speech_series.append(float(np.mean(values)) if values.size else 0.0)
    speech_seconds = min(duration, float(np.count_nonzero(speech)) * vad.hop_seconds)
    meaningful_speech = any(
        end - start >= REGION_MIN_SECONDS
        for start, end in frame_speech_intervals(vad.scores, vad.hop_seconds, 0.0)
    )
    return AudioEvidenceProfile(
        scored=True,
        reason=(
            SpeechDetectionReason.SPEECH_DETECTED
            if meaningful_speech
            else SpeechDetectionReason.NO_SPEECH
        ),
        bucket_seconds=bucket_seconds,
        speech_seconds=speech_seconds,
        longest_no_speech_seconds=min(
            duration, _longest_false_run(speech, vad.hop_seconds)
        ),
        acoustic_active_seconds=active_seconds,
        acoustic_quiet_seconds=max(0.0, duration - active_seconds),
        speech_fraction=speech_series,
        acoustic_active_fraction=acoustic_series,
        rms_dbfs=rms_series,
        peak_dbfs=peak_series,
        provider=vad.provider,
        frame_hop_ms=vad.hop_seconds * 1000.0,
    )


def detect_speech_pcm(
    pcm_data: bytes, sample_rate: int, channels: int, sample_width: int
) -> SpeechDetectionResult:
    """Whether raw PCM contains speech, for gating transcription.

    Speech means at least one frame run of ``REGION_MIN_SECONDS`` at the
    default probability threshold—the same criterion as the zero-speech
    decision in silence condensing. Unsupported inputs and VAD failures return
    an unscored result with a stable reason; callers fail open via
    ``result.should_reject``.
    """
    if sample_width != 2:
        result = SpeechDetectionResult.unscored(
            SpeechDetectionReason.UNSUPPORTED_SAMPLE_WIDTH,
            f"expected 2-byte PCM, got {sample_width}",
        )
        _log_unscored(result)
        return result
    if channels <= 0:
        result = SpeechDetectionResult.unscored(
            SpeechDetectionReason.INVALID_CHANNELS,
            f"expected positive channel count, got {channels}",
        )
        _log_unscored(result)
        return result
    if sample_rate <= 0:
        result = SpeechDetectionResult.unscored(
            SpeechDetectionReason.INVALID_SAMPLE_RATE,
            f"expected positive sample rate, got {sample_rate}",
        )
        _log_unscored(result)
        return result
    if len(pcm_data) < sample_width * channels:
        return SpeechDetectionResult.empty_audio()
    try:
        frame_scores = score_pcm_frames(pcm_data, sample_rate, channels)
    except VadScoringError as error:
        result = SpeechDetectionResult.unscored(error.reason, str(error))
        _log_unscored(result)
        return result
    intervals = frame_speech_intervals(
        frame_scores.scores,
        frame_scores.hop_seconds,
        0.0,
    )
    if any(end - start >= REGION_MIN_SECONDS for start, end in intervals):
        return SpeechDetectionResult.speech()
    return SpeechDetectionResult.no_speech()


async def analyze_conversation_audio(conversation_id: str) -> dict:
    """Run VAD over a conversation's audio.

    Writes frame scores to each chunk document and returns a dict matching
    ``Conversation.VadAnalysis`` fields. Raises ``ValueError`` if the
    conversation has no analyzable audio chunks.
    """
    start = time.time()
    provider = get_vad_provider()  # fresh instance: VAD state is per-stream
    collection = AudioChunkDocument.get_pymongo_collection()

    histogram = [0] * HISTOGRAM_BINS
    total_frames = 0
    total_duration = 0.0
    raw_intervals: List[List[float]] = []  # frame-accurate speech intervals

    batch_index = 0
    while True:
        chunks = await retrieve_audio_chunks(
            conversation_id=conversation_id,
            start_index=batch_index * BATCH_CHUNKS,
            limit=BATCH_CHUNKS,
        )
        if not chunks:
            break

        sample_rate = chunks[0].sample_rate
        channels = chunks[0].channels
        pcm = await concatenate_chunks_to_pcm(chunks)
        samples = _pcm_to_mono_int16(pcm, channels)

        # Partition the decoded stream back into chunks by expected sample
        # counts; decode drift is at most a few samples, far below the VAD
        # frame size, so boundary error is irrelevant. The final chunk takes
        # any remainder.
        updates = []
        cursor = 0
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                chunk_samples = samples[cursor:]
            else:
                n = int(round(chunk.duration * sample_rate))
                chunk_samples = samples[cursor : cursor + n]
                cursor += n

            scores = provider.score(chunk_samples, sample_rate)
            if scores.size:
                max_score = float(scores.max())
                bin_idx = np.minimum(
                    (scores / HISTOGRAM_BIN_WIDTH).astype(int), HISTOGRAM_BINS - 1
                )
                counts = np.bincount(bin_idx, minlength=HISTOGRAM_BINS)
                for b in range(HISTOGRAM_BINS):
                    histogram[b] += int(counts[b])
                total_frames += int(scores.size)
                raw_intervals.extend(
                    frame_speech_intervals(
                        scores, provider.frame_hop_ms / 1000.0, chunk.start_time
                    )
                )
            else:
                max_score = 0.0

            updates.append(
                UpdateOne(
                    {"_id": chunk.id},
                    {
                        "$set": {
                            "vad": {
                                "provider": provider.name,
                                "frame_hop_ms": provider.frame_hop_ms,
                                "scores": np.round(scores, 3).tolist(),
                                "max_score": round(max_score, 3),
                                "threshold": SPEECH_PROB_THRESHOLD,
                                "has_speech": max_score >= SPEECH_PROB_THRESHOLD,
                            }
                        }
                    },
                )
            )
            total_duration += chunk.duration

        if updates:
            await collection.bulk_write(updates, ordered=False)

        if len(chunks) < BATCH_CHUNKS:
            break
        batch_index += 1

    if total_frames == 0:
        raise ValueError(
            f"No analyzable audio for conversation {conversation_id} "
            "(no chunks or audio too short)"
        )

    result = {
        "provider": provider.name,
        "frame_hop_ms": provider.frame_hop_ms,
        "frame_count": total_frames,
        "histogram_bin_width": HISTOGRAM_BIN_WIDTH,
        "histogram": histogram,
        "chunk_duration_seconds": 10.0,
        "speech_regions": merge_speech_regions(raw_intervals, total_duration),
    }

    speech = speech_fraction_from_histogram(
        histogram, total_frames, HISTOGRAM_BIN_WIDTH, SPEECH_PROB_THRESHOLD
    )
    logger.info(
        f"🎙️ VAD analysis for {conversation_id[:12]}: "
        f"speech={speech:.1%} frames={total_frames} provider={provider.name} "
        f"({time.time() - start:.2f}s)"
    )
    return result


def frame_speech_intervals(
    scores,
    hop_seconds: float,
    offset_seconds: float,
    threshold: float = SPEECH_PROB_THRESHOLD,
) -> List[List[float]]:
    """Raw [start, end] intervals (seconds) where consecutive frame
    probabilities are >= ``threshold``. ``offset_seconds`` is the absolute
    time of the first frame (e.g. the chunk's start_time)."""
    intervals: List[List[float]] = []
    run_start: Optional[int] = None
    n = len(scores)
    for i in range(n):
        if scores[i] >= threshold:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            intervals.append(
                [
                    offset_seconds + run_start * hop_seconds,
                    offset_seconds + i * hop_seconds,
                ]
            )
            run_start = None
    if run_start is not None:
        intervals.append(
            [offset_seconds + run_start * hop_seconds, offset_seconds + n * hop_seconds]
        )
    return intervals


def merge_speech_regions(
    intervals: List[List[float]],
    duration_seconds: float,
    pad_seconds: float = REGION_PAD_SECONDS,
    merge_gap_seconds: float = REGION_MERGE_GAP_SECONDS,
    min_region_seconds: float = REGION_MIN_SECONDS,
    max_count: int = REGION_MAX_COUNT,
) -> List[List[float]]:
    """Pad, merge and prune raw speech intervals into playback regions.

    Regions are padded by ``pad_seconds`` on both sides (clamped to the
    conversation), merged when separated by less than ``merge_gap_seconds``,
    and blips shorter than ``min_region_seconds`` (pre-padding) are dropped.
    If more than ``max_count`` regions remain, the merge gap doubles until
    the list fits — long recordings stay compact.
    """
    kept = [iv for iv in intervals if iv[1] - iv[0] >= min_region_seconds]
    if not kept:
        return []
    kept.sort()

    gap = merge_gap_seconds
    while True:
        merged: List[List[float]] = []
        for start, end in kept:
            start = max(0.0, start - pad_seconds)
            end = min(duration_seconds, end + pad_seconds)
            if merged and start - merged[-1][1] < gap:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        if len(merged) <= max_count:
            return [[round(s, 2), round(e, 2)] for s, e in merged]
        gap *= 2


def intersect_intervals(
    a: List[List[float]],
    b: List[List[float]],
) -> List[List[float]]:
    """Intersection of two [start, end] interval lists (seconds).

    Used to combine frame-level VAD speech intervals with speaker-tagged
    transcript segments: the result keeps only the time both signals agree on
    (the speaker was tagged AND the VAD heard voice). Inputs may be unsorted
    and internally overlapping; each side is unioned first. The result is
    sorted and disjoint.
    """

    def _union(intervals: List[List[float]]) -> List[List[float]]:
        out: List[List[float]] = []
        for start, end in sorted([iv[0], iv[1]] for iv in intervals if iv[1] > iv[0]):
            if out and start <= out[-1][1]:
                out[-1][1] = max(out[-1][1], end)
            else:
                out.append([start, end])
        return out

    a, b = _union(a), _union(b)
    result: List[List[float]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if end > start:
            result.append([start, end])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return result


def subtract_intervals(
    regions: List[List[float]],
    cuts: List[List[float]],
    min_region_seconds: float = 0.05,
) -> List[List[float]]:
    """Remove ``cuts`` time ranges from ``regions``, splitting where needed.

    Used by the annotation export to drop privacy-screened segments: each
    excluded ``[start, end]`` is carved out of the speech/full regions so the
    cut audio never makes it into a clip. Slivers shorter than
    ``min_region_seconds`` are discarded. Both lists are in absolute
    conversation seconds; the result is sorted and disjoint.
    """
    if not cuts:
        return [list(r) for r in regions]

    # Merge overlapping/adjacent cuts so the carve loop is single-pass.
    ordered = sorted([c[0], c[1]] for c in cuts if c[1] > c[0])
    merged_cuts: List[List[float]] = []
    for start, end in ordered:
        if merged_cuts and start <= merged_cuts[-1][1]:
            merged_cuts[-1][1] = max(merged_cuts[-1][1], end)
        else:
            merged_cuts.append([start, end])

    result: List[List[float]] = []
    for r_start, r_end in regions:
        cursor = r_start
        for c_start, c_end in merged_cuts:
            if c_end <= cursor or c_start >= r_end:
                continue
            if c_start > cursor:
                result.append([cursor, min(c_start, r_end)])
            cursor = max(cursor, c_end)
            if cursor >= r_end:
                break
        if cursor < r_end:
            result.append([cursor, r_end])

    return [
        [round(s, 2), round(e, 2)] for s, e in result if e - s >= min_region_seconds
    ]


def speech_fraction_from_histogram(
    histogram: List[int],
    frame_count: int,
    histogram_bin_width: float,
    threshold: float,
) -> float:
    """Fraction of frames with speech probability >= ``threshold`` (0.0-1.0)."""
    if frame_count <= 0 or not histogram:
        return 0.0
    speech = 0
    for i, count in enumerate(histogram):
        bin_lower = i * histogram_bin_width
        if bin_lower >= threshold:
            speech += count
    return speech / frame_count


def detect_silence_gaps(
    chunks: List[Dict[str, Any]],
    speech_threshold: float = SPEECH_PROB_THRESHOLD,
    min_gap_seconds: float = 900.0,
) -> List[Dict[str, float]]:
    """Find long speech-free gaps in a conversation's chunk timeline.

    ``chunks`` is chunk metadata sorted by ``chunk_index``, each with
    ``chunk_index``, ``start_time``, ``end_time`` and a ``vad`` subdocument
    (only ``vad.max_score`` is needed). A chunk counts as silent when its max
    VAD score is below ``speech_threshold``; unanalyzed chunks (no ``vad``)
    count as speech so a split is never suggested through unscored audio.

    Runs touching the start or end of the conversation are excluded — they
    are leading/trailing silence, not split points. Returns gaps of at least
    ``min_gap_seconds`` as dicts with ``start_seconds``, ``end_seconds``,
    ``duration_seconds``, ``start_chunk``, ``end_chunk`` and
    ``split_point_seconds`` (the gap end, where speech resumes).
    """
    if not chunks:
        return []

    def _is_silent(c: Dict[str, Any]) -> bool:
        score: Optional[float] = (c.get("vad") or {}).get("max_score")
        return score is not None and score < speech_threshold

    gaps: List[Dict[str, float]] = []
    run_start: Optional[int] = None  # index into chunks list

    for i, chunk in enumerate(chunks):
        if _is_silent(chunk):
            if run_start is None:
                run_start = i
            continue
        if run_start is not None:
            if run_start > 0:  # exclude leading-silence run
                _append_gap(gaps, chunks, run_start, i - 1, min_gap_seconds)
            run_start = None

    # A trailing run (run_start still open at loop end) touches the end of the
    # conversation: excluded by design.
    return gaps


def _append_gap(
    gaps: List[Dict[str, float]],
    chunks: List[Dict[str, Any]],
    first: int,
    last: int,
    min_gap_seconds: float,
) -> None:
    start_s = float(chunks[first]["start_time"])
    end_s = float(chunks[last]["end_time"])
    if end_s - start_s < min_gap_seconds:
        return
    gaps.append(
        {
            "start_seconds": round(start_s, 2),
            "end_seconds": round(end_s, 2),
            "duration_seconds": round(end_s - start_s, 2),
            "start_chunk": int(chunks[first]["chunk_index"]),
            "end_chunk": int(chunks[last]["chunk_index"]),
            "split_point_seconds": round(end_s, 2),
        }
    )
