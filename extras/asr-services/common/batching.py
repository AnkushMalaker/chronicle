"""
Audio batching utilities for long-form transcription.

Splits long audio files into overlapping windows, transcribes each window,
and stitches results back together with overlap deduplication.

Used by ASR providers that need to batch long audio internally (e.g., VibeVoice
on a single GPU can handle ~5 min clips but not 30+ min files).
"""

import logging
import os
import tempfile
import wave
from typing import List, Optional, Tuple

import numpy as np

from common.audio_utils import STANDARD_SAMPLE_RATE, load_audio_file, numpy_to_audio_bytes
from common.response_models import Segment, Speaker, TranscriptionResult, Word

logger = logging.getLogger(__name__)


def split_audio_file(
    audio_path: str,
    batch_duration: float = 240.0,
    overlap: float = 30.0,
    sample_rate: int = STANDARD_SAMPLE_RATE,
) -> List[Tuple[str, float, float]]:
    """
    Split a long audio file into overlapping windows saved as temp WAV files.

    Each window is batch_duration + overlap seconds long (except the last).
    Windows advance by batch_duration seconds, so consecutive windows share
    an overlap region of `overlap` seconds.

    Example for 12-minute audio with batch_duration=240, overlap=30:
        Window 0: [0:00 - 4:30]
        Window 1: [4:00 - 8:30]
        Window 2: [8:00 - 12:00]

    Args:
        audio_path: Path to the input audio file.
        batch_duration: Length of each non-overlapping window in seconds.
        overlap: Overlap between consecutive windows in seconds.
        sample_rate: Target sample rate for output files.

    Returns:
        List of (temp_file_path, start_time, end_time) tuples.
        Caller is responsible for deleting temp files.
    """
    audio_array, sr = load_audio_file(audio_path, target_rate=sample_rate)
    total_samples = len(audio_array)
    total_duration = total_samples / sample_rate

    logger.info(
        f"Splitting audio: {total_duration:.1f}s into {batch_duration}s windows "
        f"with {overlap}s overlap"
    )

    batch_samples = int(batch_duration * sample_rate)
    overlap_samples = int(overlap * sample_rate)
    window_samples = batch_samples + overlap_samples

    segments = []
    offset = 0

    while offset < total_samples:
        # Extract window: batch_duration + overlap (or whatever is left)
        end_sample = min(offset + window_samples, total_samples)
        window = audio_array[offset:end_sample]

        start_time = offset / sample_rate
        end_time = end_sample / sample_rate

        # Save to temp WAV
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        audio_bytes = numpy_to_audio_bytes(window, sample_width=2)
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_bytes)

        segments.append((tmp.name, start_time, end_time))
        logger.info(f"  Window {len(segments)-1}: [{start_time:.1f}s - {end_time:.1f}s]")

        # Advance by batch_duration (not window size) so next window overlaps
        offset += batch_samples

        # If the remaining audio is shorter than the overlap, we already captured it
        if total_samples - offset <= overlap_samples:
            break

    logger.info(f"Split into {len(segments)} windows")
    return segments


def stitch_transcription_results(
    batch_results: List[Tuple[TranscriptionResult, float, float]],
    overlap_seconds: float,
) -> TranscriptionResult:
    """
    Stitch multiple batch transcription results into a single result.

    For overlapping regions between consecutive batches, uses a midpoint
    deduplication strategy: segments whose midpoint falls before the overlap
    midpoint are kept from the earlier batch; those after are kept from the
    later batch.

    Args:
        batch_results: List of (TranscriptionResult, start_time, end_time) tuples.
            start_time/end_time are the absolute times of this batch in the
            original audio.
        overlap_seconds: Overlap duration between consecutive batches.

    Returns:
        Unified TranscriptionResult with deduplicated, time-corrected segments.
    """
    if not batch_results:
        return TranscriptionResult(text="", words=[], segments=[])

    if len(batch_results) == 1:
        result, start, end = batch_results[0]
        return TranscriptionResult(
            text=result.text,
            words=_offset_words(result.words, start),
            segments=_offset_segments(result.segments, start),
            speakers=result.speakers,
            language=result.language,
            duration=end,
        )

    all_segments: List[Segment] = []
    all_words: List[Word] = []
    all_speakers: dict[str, Tuple[float, float]] = {}

    for i, (result, batch_start, batch_end) in enumerate(batch_results):
        # Offset all timestamps to absolute time
        offset_segs = _offset_segments(result.segments, batch_start)
        offset_words = _offset_words(result.words, batch_start)

        if i == 0:
            # First batch: keep segments before the overlap midpoint with next batch
            if len(batch_results) > 1:
                _, next_start, _ = batch_results[1]
                cutoff = next_start + overlap_seconds / 2
                offset_segs = [s for s in offset_segs if _seg_midpoint(s) < cutoff]
                offset_words = [w for w in offset_words if _word_midpoint(w) < cutoff]
        elif i == len(batch_results) - 1:
            # Last batch: keep segments after the overlap midpoint with previous batch
            _, prev_start, prev_end = batch_results[i - 1]
            cutoff = batch_start + overlap_seconds / 2
            offset_segs = [s for s in offset_segs if _seg_midpoint(s) >= cutoff]
            offset_words = [w for w in offset_words if _word_midpoint(w) >= cutoff]
        else:
            # Middle batch: trim both sides
            _, prev_start, prev_end = batch_results[i - 1]
            left_cutoff = batch_start + overlap_seconds / 2
            _, next_start, _ = batch_results[i + 1]
            right_cutoff = next_start + overlap_seconds / 2
            offset_segs = [
                s for s in offset_segs
                if _seg_midpoint(s) >= left_cutoff and _seg_midpoint(s) < right_cutoff
            ]
            offset_words = [
                w for w in offset_words
                if _word_midpoint(w) >= left_cutoff and _word_midpoint(w) < right_cutoff
            ]

        all_segments.extend(offset_segs)
        all_words.extend(offset_words)

        # Merge speaker info
        if result.speakers:
            for spk in result.speakers:
                abs_start = spk.start + batch_start
                abs_end = spk.end + batch_start
                if spk.id in all_speakers:
                    prev_s, prev_e = all_speakers[spk.id]
                    all_speakers[spk.id] = (min(prev_s, abs_start), max(prev_e, abs_end))
                else:
                    all_speakers[spk.id] = (abs_start, abs_end)

    # Build final text from segments
    text = " ".join(s.text for s in all_segments if s.text.strip())

    # Build speaker list
    speakers = [
        Speaker(id=spk_id, start=times[0], end=times[1])
        for spk_id, times in all_speakers.items()
    ] if all_speakers else None

    # Duration from last segment
    duration = max(s.end for s in all_segments) if all_segments else None

    logger.info(
        f"Stitched {len(batch_results)} batches: "
        f"{len(all_segments)} segments, {len(all_words)} words"
    )

    return TranscriptionResult(
        text=text,
        words=all_words,
        segments=all_segments,
        speakers=speakers,
        language=batch_results[0][0].language,
        duration=duration,
    )


def extract_context_tail(result: TranscriptionResult, max_chars: int = 500) -> str:
    """
    Extract the last N characters of transcript text for context passing.

    Used to provide the next batch window with context from the previous
    window's transcription, improving continuity.

    Args:
        result: Transcription result from the previous batch.
        max_chars: Maximum characters to extract.

    Returns:
        Tail of the transcript text, or empty string if no text.
    """
    if result.segments:
        text = " ".join(s.text for s in result.segments if s.text.strip())
    else:
        text = result.text

    if not text:
        return ""

    return text[-max_chars:]


def _offset_segments(segments: List[Segment], offset: float) -> List[Segment]:
    """Offset all segment timestamps by the given amount."""
    return [
        Segment(
            text=s.text,
            start=s.start + offset,
            end=s.end + offset,
            speaker=s.speaker,
        )
        for s in segments
    ]


def _offset_words(words: List[Word], offset: float) -> List[Word]:
    """Offset all word timestamps by the given amount."""
    return [
        Word(
            word=w.word,
            start=w.start + offset,
            end=w.end + offset,
            confidence=w.confidence,
        )
        for w in words
    ]


def _seg_midpoint(seg: Segment) -> float:
    """Get the temporal midpoint of a segment."""
    return (seg.start + seg.end) / 2


def _word_midpoint(word: Word) -> float:
    """Get the temporal midpoint of a word."""
    return (word.start + word.end) / 2
