"""Client for the Chronicle TTS service (text → speech WAV bytes).

Resolves the TTS endpoint the same way other services are resolved
(``CHRONICLE_TTS_URL``/``TTS_URL`` env override → minidisc/Tailscale discovery)
and calls its ``POST /synthesize`` endpoint. All failures degrade to ``None`` so
callers can treat speech output as best-effort.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_TTS_TIMEOUT = float(os.getenv("TTS_TIMEOUT", "30"))


def _resolve_tts_url() -> str | None:
    """Resolve the TTS base URL (env override → minidisc discovery → None)."""
    url = os.getenv("CHRONICLE_TTS_URL") or os.getenv("TTS_URL")
    if url:
        return url
    try:
        from discovery import CHRONICLE_TTS, resolve_service_url

        return resolve_service_url(None, CHRONICLE_TTS, default=None)
    except ImportError:
        return None


async def synthesize_speech(text: str) -> bytes | None:
    """Synthesize ``text`` to WAV bytes. Returns ``None`` if disabled or on failure."""
    text = (text or "").strip()
    if not text:
        return None

    base_url = _resolve_tts_url()
    if not base_url:
        logger.info(
            "TTS not configured (set CHRONICLE_TTS_URL) — skipping speech synthesis"
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/synthesize",
                data={"text": text},
            )
            resp.raise_for_status()
            return resp.content
    except Exception as e:  # noqa: BLE001 - speech output is best-effort
        logger.error(f"TTS synthesis failed via {base_url}: {e}", exc_info=True)
        return None
