"""Forced-alignment client: synthesize word timestamps from segment text + audio.

Some ASR/diarization providers (e.g. VibeVoice) emit segment-level timestamps and
text but no per-word timing. Word timings are required to re-attach transcript text
to freshly re-diarized speaker boundaries (the pyannote "Path A" speaker pipeline).

This calls the running ASR service's ``/align`` endpoint (MMS_FA forced aligner),
sending the full conversation audio + segment {start, end, text}, and returns
word dicts with absolute timestamps. Returns [] on any failure so callers can fall
back to segment-level identification.
"""

import json
import logging
from typing import Dict, List
from urllib.parse import urlparse, urlunparse

import httpx

from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_audio_segment

logger = logging.getLogger(__name__)


def _align_url() -> str:
    """Derive the ASR service's /align URL from the configured batch STT model."""
    registry = get_models_registry()
    model = registry.get_default("stt") if registry else None
    if not model:
        return ""
    base = model.resolved_url()
    if not base:
        return ""
    parsed = urlparse(base)
    if not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, "/align", "", "", ""))


async def synthesize_words_via_alignment(
    conversation_id: str,
    segments: List[Dict],
    total_duration: float,
    timeout: float = 600.0,
) -> List[Dict]:
    """Forced-align segment text to conversation audio → word-level timestamps.

    Args:
        conversation_id: conversation to fetch audio for
        segments: list of {start, end, text} (speech segments with non-empty text)
        total_duration: seconds of audio to fetch (cover all segments)

    Returns:
        List of {word, start, end, confidence} with absolute timestamps, or [] on
        failure / when no aligner is reachable.
    """
    if not segments or total_duration <= 0:
        return []

    try:
        wav_bytes = await reconstruct_audio_segment(
            conversation_id, 0.0, total_duration
        )
    except Exception as e:
        logger.warning(f"🔤 Could not fetch audio for alignment: {e}")
        return []

    return await align_audio_words(wav_bytes, segments, timeout=timeout)


async def align_audio_words(
    wav_bytes: bytes,
    segments: List[Dict],
    timeout: float = 600.0,
) -> List[Dict]:
    """Forced-align known segment text against an in-memory WAV."""
    if not wav_bytes or not segments:
        return []

    url = _align_url()
    if not url:
        logger.warning(
            "🔤 No /align URL resolvable from batch STT model; skipping alignment"
        )
        return []

    seg_payload = [
        {"start": float(s["start"]), "end": float(s["end"]), "text": s.get("text", "")}
        for s in segments
    ]

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(
                url,
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={"segments": json.dumps(seg_payload)},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"🔤 Forced-alignment request to {url} failed: {e}")
        return []

    words = data.get("words", [])
    logger.info(
        f"🔤 Forced alignment returned {len(words)} words "
        f"({data.get('aligned_segments')}/{data.get('total_segments')} segments aligned)"
    )
    return words
