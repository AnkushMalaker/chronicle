"""
Amplitude / near-silence analysis for conversation audio.

Decodes the Opus chunks stored in MongoDB, computes per-window RMS loudness,
and summarizes it into threshold-independent metrics (mean/peak dBFS plus a
dBFS histogram). The histogram lets callers derive the "silent fraction" for
any amplitude threshold without re-decoding the audio.

Used by the Data Cleaning feature to surface which conversations are mostly
background/near-silence so their audio can be archived (hard-deleted).
"""

import logging
import time

import numpy as np

from advanced_omi_backend.utils.audio_chunk_utils import (
    concatenate_chunks_to_pcm,
    retrieve_audio_chunks,
)

logger = logging.getLogger(__name__)

# Analysis parameters
WINDOW_MS = 100  # window size for RMS computation
BATCH_CHUNKS = 30  # decode 30 x 10s chunks (~5 min) at a time to bound memory
HISTOGRAM_MIN_DBFS = -90.0  # quietest bin edge (digital silence floor)
HISTOGRAM_BIN_WIDTH = 3.0  # dB per bin
HISTOGRAM_BINS = int(
    round(-HISTOGRAM_MIN_DBFS / HISTOGRAM_BIN_WIDTH)
)  # 30 bins: -90..0

# Floor used when a window is pure digital silence (rms == 0)
DBFS_FLOOR = HISTOGRAM_MIN_DBFS


def _windows_to_dbfs(pcm: bytes, sample_rate: int, channels: int) -> np.ndarray:
    """Compute per-window RMS loudness (dBFS) for a PCM buffer.

    Returns a 1-D float array, one dBFS value per ``WINDOW_MS`` window. The
    final partial window (if any) is dropped to avoid skewing short tails.
    """
    if not pcm:
        return np.empty(0, dtype=np.float64)

    samples = np.frombuffer(pcm, dtype=np.int16)
    if channels > 1:
        # Average channels down to mono before measuring loudness
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)

    samples_per_window = max(1, int(sample_rate * WINDOW_MS / 1000))
    n_windows = samples.size // samples_per_window
    if n_windows == 0:
        return np.empty(0, dtype=np.float64)

    trimmed = samples[: n_windows * samples_per_window].astype(np.float64)
    windows = trimmed.reshape(n_windows, samples_per_window)

    rms = np.sqrt(np.mean(np.square(windows), axis=1))
    # dBFS relative to full-scale 16-bit (32768). Guard against log(0).
    with np.errstate(divide="ignore"):
        dbfs = 20.0 * np.log10(rms / 32768.0)
    dbfs = np.where(np.isfinite(dbfs), dbfs, DBFS_FLOOR)
    return np.clip(dbfs, DBFS_FLOOR, 0.0)


async def analyze_conversation_audio(conversation_id: str) -> dict:
    """Analyze a conversation's audio amplitude.

    Returns a dict matching ``Conversation.SilenceAnalysis`` fields, or raises
    ``ValueError`` if the conversation has no audio chunks.
    """
    start = time.time()

    histogram = [0] * HISTOGRAM_BINS
    total_windows = 0
    dbfs_sum = 0.0
    peak_dbfs = DBFS_FLOOR
    total_duration = 0.0

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
        dbfs = _windows_to_dbfs(pcm, sample_rate, channels)

        if dbfs.size:
            total_windows += int(dbfs.size)
            dbfs_sum += float(dbfs.sum())
            peak_dbfs = max(peak_dbfs, float(dbfs.max()))
            # Bin into the fixed histogram
            bin_idx = ((dbfs - HISTOGRAM_MIN_DBFS) / HISTOGRAM_BIN_WIDTH).astype(int)
            bin_idx = np.clip(bin_idx, 0, HISTOGRAM_BINS - 1)
            counts = np.bincount(bin_idx, minlength=HISTOGRAM_BINS)
            for i in range(HISTOGRAM_BINS):
                histogram[i] += int(counts[i])

        total_duration += sum(c.duration for c in chunks)

        if len(chunks) < BATCH_CHUNKS:
            break
        batch_index += 1

    if total_windows == 0:
        raise ValueError(
            f"No analyzable audio for conversation {conversation_id} "
            "(no chunks or audio too short)"
        )

    mean_dbfs = dbfs_sum / total_windows

    result = {
        "duration_seconds": round(total_duration, 2),
        "mean_dbfs": round(mean_dbfs, 2),
        "peak_dbfs": round(peak_dbfs, 2),
        "window_ms": WINDOW_MS,
        "window_count": total_windows,
        "histogram_min_dbfs": HISTOGRAM_MIN_DBFS,
        "histogram_bin_width": HISTOGRAM_BIN_WIDTH,
        "histogram": histogram,
    }

    logger.info(
        f"🔇 Silence analysis for {conversation_id[:12]}: "
        f"mean={mean_dbfs:.1f}dBFS peak={peak_dbfs:.1f}dBFS "
        f"windows={total_windows} ({time.time() - start:.2f}s)"
    )
    return result


def silent_fraction_from_histogram(
    histogram: list[int],
    window_count: int,
    histogram_min_dbfs: float,
    histogram_bin_width: float,
    threshold_dbfs: float,
) -> float:
    """Derive the fraction of windows quieter than ``threshold_dbfs`` (0.0-1.0)."""
    if window_count <= 0 or not histogram:
        return 0.0
    silent = 0
    for i, count in enumerate(histogram):
        bin_upper = histogram_min_dbfs + (i + 1) * histogram_bin_width
        if bin_upper <= threshold_dbfs:
            silent += count
    return silent / window_count
