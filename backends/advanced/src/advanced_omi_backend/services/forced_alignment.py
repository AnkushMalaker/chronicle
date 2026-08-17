"""Forced-alignment client: synthesize word timestamps from segment text + audio.

Some ASR/diarization providers (e.g. VibeVoice) emit segment-level timestamps and
text but no per-word timing. Word timings are required to re-attach transcript text
to freshly re-diarized speaker boundaries (the pyannote "Path A" speaker pipeline).

This calls a configured Chronicle ASR service that explicitly advertises the
``forced_alignment`` capability, using its ``/align`` endpoint (MMS_FA forced aligner).
It sends the full conversation audio plus segment {start, end, text} and returns word
dicts with absolute timestamps. Returns [] on failure so callers can persist an
explicitly marked segment-clock estimate instead.
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
    if "forced_alignment" not in model.capabilities:
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


def estimate_words_from_segment_timing(segments: List[Dict]) -> List[Dict]:
    """Create monotonic word clocks when the neural aligner cannot align text.

    Segment timestamps remain authoritative; this only distributes their words
    uniformly inside each segment so Pyannote speaker turns can receive the existing
    transcript text.  It is deliberately a last resort after forced alignment.
    """
    words: List[Dict] = []
    for segment in segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        tokens = str(segment.get("text", "")).split()
        if not tokens or end <= start:
            continue
        step = (end - start) / len(tokens)
        for index, token in enumerate(tokens):
            words.append(
                {
                    "word": token,
                    "start": start + index * step,
                    "end": start + (index + 1) * step,
                    "confidence": 0.0,
                }
            )
    return words
