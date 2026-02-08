"""
Waveform generation workers for audio visualization.

This module provides async functions to generate waveform data from
audio chunks stored in MongoDB. Waveforms are computed on-demand
and cached for subsequent requests.
"""

import logging
import struct
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


async def generate_waveform_data(
    conversation_id: str,
    sample_rate: int = 3,
) -> Dict[str, Any]:
    """
    Generate waveform visualization data from conversation audio chunks.

    This function:
    1. Retrieves Opus-compressed audio chunks from MongoDB
    2. Decodes each chunk to PCM
    3. Downsamples PCM to target sample rate (e.g., 10 samples/sec)
    4. Calculates amplitude peaks for each sample window
    5. Normalizes to [-1.0, 1.0] range
    6. Stores in WaveformData collection

    Args:
        conversation_id: Conversation ID to generate waveform for
        sample_rate: Samples per second for waveform (default: 10)

    Returns:
        Dict with:
            - success: bool
            - samples: List[float] (if successful)
            - sample_rate: int (if successful)
            - duration_seconds: float (if successful)
            - error: str (if failed)
    """
    from advanced_omi_backend.models.waveform import WaveformData
    from advanced_omi_backend.utils.audio_chunk_utils import (
        decode_opus_to_pcm,
        retrieve_audio_chunks,
    )

    start_time = time.time()
    fetch_time = 0.0
    decode_time = 0.0
    waveform_gen_time = 0.0

    try:
        logger.info(f"🎵 Generating waveform for conversation {conversation_id[:12]}... (sample_rate={sample_rate} samples/sec)")

        # Retrieve all audio chunks for conversation
        fetch_start = time.time()
        chunks = await retrieve_audio_chunks(conversation_id=conversation_id)
        fetch_time = time.time() - fetch_start

        logger.info(f"📦 Fetched {len(chunks) if chunks else 0} chunks from MongoDB in {fetch_time:.2f}s")

        if not chunks:
            logger.warning(f"No audio chunks found for conversation {conversation_id}")
            return {
                "success": False,
                "error": "No audio chunks found for this conversation"
            }

        # Get audio format from first chunk
        pcm_sample_rate = chunks[0].sample_rate  # Usually 16000 Hz
        channels = chunks[0].channels  # Usually 1 (mono)
        bytes_per_sample = 2  # 16-bit PCM

        # Calculate total duration
        total_duration = sum(chunk.duration for chunk in chunks)

        # Calculate window size for downsampling
        # e.g., 16000 samples/sec ÷ 10 waveform_samples/sec = 1600 PCM samples per waveform point
        window_size_samples = pcm_sample_rate // sample_rate
        bytes_per_window = window_size_samples * bytes_per_sample * channels

        logger.info(
            f"Processing {len(chunks)} chunks, "
            f"total duration: {total_duration:.1f}s, "
            f"window size: {window_size_samples} samples"
        )

        # Process chunks and extract amplitude peaks
        waveform_samples: List[float] = []

        for chunk_idx, chunk in enumerate(chunks):
            # Decode Opus to PCM
            decode_start = time.time()
            pcm_data = await decode_opus_to_pcm(
                opus_data=chunk.audio_data,
                sample_rate=pcm_sample_rate,
                channels=channels,
            )
            decode_time += time.time() - decode_start

            # Process PCM data in windows
            waveform_gen_start = time.time()
            offset = 0
            while offset < len(pcm_data):
                # Extract window
                window_end = min(offset + bytes_per_window, len(pcm_data))
                window_bytes = pcm_data[offset:window_end]

                if len(window_bytes) == 0:
                    break

                # Convert bytes to signed 16-bit integers
                num_samples_in_window = len(window_bytes) // bytes_per_sample
                format_str = f"{num_samples_in_window}h"  # 'h' = signed short (16-bit)

                try:
                    pcm_samples = struct.unpack(format_str, window_bytes)
                except struct.error as e:
                    logger.warning(f"Struct unpack error: {e}, skipping window")
                    offset += bytes_per_window
                    continue

                # Calculate peak amplitude in this window
                # Normalize from 16-bit range (-32768 to 32767) to [-1.0, 1.0]
                if pcm_samples:
                    max_abs_amplitude = max(abs(s) for s in pcm_samples)
                    normalized_amplitude = max_abs_amplitude / 32768.0
                    waveform_samples.append(normalized_amplitude)

                offset += bytes_per_window

            waveform_gen_time += time.time() - waveform_gen_start

            # Log progress for long conversations
            if (chunk_idx + 1) % 20 == 0:
                logger.info(
                    f"Processed {chunk_idx + 1}/{len(chunks)} chunks "
                    f"({len(waveform_samples)} waveform samples so far)"
                )

        processing_time = time.time() - start_time
        other_time = processing_time - (fetch_time + decode_time + waveform_gen_time)

        logger.info(
            f"✅ Generated waveform: {len(waveform_samples)} samples "
            f"for {total_duration:.1f}s audio in {processing_time:.2f}s total"
        )
        logger.info(
            f"   ⏱️  Timing breakdown: "
            f"Fetch={fetch_time:.2f}s, "
            f"Decode={decode_time:.2f}s, "
            f"Waveform={waveform_gen_time:.2f}s, "
            f"Other={other_time:.2f}s"
        )

        # Store in MongoDB
        waveform_doc = WaveformData(
            conversation_id=conversation_id,
            samples=waveform_samples,
            sample_rate=sample_rate,
            duration_seconds=total_duration,
            processing_time_seconds=processing_time
        )

        await waveform_doc.insert()

        logger.info(f"💾 Saved waveform to MongoDB for conversation {conversation_id[:12]}")

        return {
            "success": True,
            "samples": waveform_samples,
            "sample_rate": sample_rate,
            "duration_seconds": total_duration,
            "processing_time_seconds": processing_time
        }

    except Exception as e:
        processing_time = time.time() - start_time
        logger.error(
            f"❌ Waveform generation failed for {conversation_id}: {e}",
            exc_info=True
        )
        return {
            "success": False,
            "error": str(e),
            "processing_time_seconds": processing_time
        }
