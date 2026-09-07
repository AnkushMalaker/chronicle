"""
Waveform generation workers for audio visualization.

This module provides async functions to generate waveform data from
audio chunks stored in MongoDB. Waveforms are computed on-demand
and cached for subsequent requests.

Audio is processed in 5-minute batches (30 x 10s chunks) to bound memory usage.
"""

import logging
import struct
import time
from typing import Any, Dict, List, Optional

from backend.models.waveform import WaveformData
from backend.utils.audio_chunk_utils import decode_opus_to_pcm, retrieve_audio_chunks

logger = logging.getLogger(__name__)

# 5 minutes of 10s chunks
BATCH_SIZE = 30
BYTES_PER_SAMPLE = 2  # 16-bit PCM


def _pcm_to_peaks(pcm_data: bytes, bytes_per_window: int) -> List[float]:
    """Extract normalized peak amplitudes from PCM data at the given window size."""
    peaks: List[float] = []
    offset = 0

    while offset < len(pcm_data):
        window_bytes = pcm_data[offset : offset + bytes_per_window]
        if not window_bytes:
            break

        n_samples = len(window_bytes) // BYTES_PER_SAMPLE
        try:
            samples = struct.unpack(f"{n_samples}h", window_bytes)
        except struct.error:
            offset += bytes_per_window
            continue

        if samples:
            peaks.append(max(abs(s) for s in samples) / 32768.0)

        offset += bytes_per_window

    return peaks


async def _process_batch(
    conversation_id: str,
    batch_index: int,
    sample_rate: int,
    pcm_sample_rate: Optional[int],
    channels: Optional[int],
) -> tuple[List[float], float, int, int, int, float, float]:
    """
    Fetch and process one batch of chunks.

    Returns (peaks, duration, chunk_count, pcm_sample_rate, channels, fetch_dt, decode_dt).
    """
    start_index = batch_index * BATCH_SIZE

    fetch_start = time.time()
    chunks = await retrieve_audio_chunks(
        conversation_id=conversation_id,
        start_index=start_index,
        limit=BATCH_SIZE,
    )
    fetch_dt = time.time() - fetch_start

    if not chunks:
        return [], 0.0, 0, pcm_sample_rate or 0, channels or 0, fetch_dt, 0.0

    # Derive format from first chunk if not yet known
    if pcm_sample_rate is None:
        pcm_sample_rate = chunks[0].sample_rate
        channels = chunks[0].channels

    window_bytes = (pcm_sample_rate // sample_rate) * BYTES_PER_SAMPLE * channels
    batch_peaks: List[float] = []
    batch_duration = 0.0
    decode_dt = 0.0

    for chunk in chunks:
        t0 = time.time()
        pcm_data = await decode_opus_to_pcm(
            opus_data=chunk.audio_data,
            sample_rate=pcm_sample_rate,
            channels=channels,
        )
        decode_dt += time.time() - t0

        batch_peaks.extend(_pcm_to_peaks(pcm_data, window_bytes))
        batch_duration += chunk.duration

    return (
        batch_peaks,
        batch_duration,
        len(chunks),
        pcm_sample_rate,
        channels,
        fetch_dt,
        decode_dt,
    )


async def generate_waveform_data(
    conversation_id: str,
    sample_rate: int = 3,
) -> Dict[str, Any]:
    """
    Generate waveform visualization data from conversation audio chunks.

    Processes chunks in 5-minute batches (30 x 10s chunks) to keep memory bounded.
    Each batch is fetched, decoded, downsampled to `sample_rate` peaks/sec,
    and the results are concatenated into the final waveform.

    Returns dict with success/samples/sample_rate/duration_seconds or success/error.
    """
    start_time = time.time()
    total_fetch = 0.0
    total_decode = 0.0

    try:
        logger.info(
            f"Generating waveform for {conversation_id[:12]}... ({sample_rate} samples/sec)"
        )

        all_peaks: List[float] = []
        total_duration = 0.0
        total_chunks = 0
        pcm_sr: Optional[int] = None
        ch: Optional[int] = None
        batch_idx = 0

        while True:
            peaks, dur, n, pcm_sr, ch, f_dt, d_dt = await _process_batch(
                conversation_id,
                batch_idx,
                sample_rate,
                pcm_sr,
                ch,
            )
            total_fetch += f_dt
            total_decode += d_dt

            if not peaks and batch_idx == 0:
                logger.warning(
                    f"No audio chunks found for conversation {conversation_id}"
                )
                return {
                    "success": False,
                    "error": "No audio chunks found for this conversation",
                }

            if not peaks:
                break

            all_peaks.extend(peaks)
            total_duration += dur
            total_chunks += n

            logger.info(
                f"Batch {batch_idx}: {n} chunks, {dur:.0f}s, {len(peaks)} peaks"
            )
            batch_idx += 1

            if n < BATCH_SIZE:
                break

        processing_time = time.time() - start_time
        logger.info(
            f"Waveform done: {len(all_peaks)} samples, {total_duration:.0f}s audio, "
            f"{total_chunks} chunks in {batch_idx} batches ({processing_time:.2f}s) "
            f"[fetch={total_fetch:.2f}s decode={total_decode:.2f}s]"
        )

        waveform_doc = WaveformData(
            conversation_id=conversation_id,
            samples=all_peaks,
            sample_rate=sample_rate,
            duration_seconds=total_duration,
            processing_time_seconds=processing_time,
        )
        await waveform_doc.insert()

        return {
            "success": True,
            "samples": all_peaks,
            "sample_rate": sample_rate,
            "duration_seconds": total_duration,
            "processing_time_seconds": processing_time,
        }

    except Exception as e:
        processing_time = time.time() - start_time
        logger.error(
            f"Waveform generation failed for {conversation_id}: {e}", exc_info=True
        )
        return {
            "success": False,
            "error": str(e),
            "processing_time_seconds": processing_time,
        }
