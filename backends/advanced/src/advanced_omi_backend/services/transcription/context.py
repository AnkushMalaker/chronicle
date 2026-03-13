"""
ASR context builder for transcription.

Combines static hot words from the prompt registry with per-user dynamic
jargon cached in Redis by the ``asr_jargon_extraction`` cron job.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


@dataclass
class TranscriptionContext:
    """Structured context gathered before transcription.

    Holds the individual context components so callers can inspect them
    and Langfuse spans can log them as structured metadata.
    """

    hot_words: str = ""
    user_jargon: str = ""
    user_id: Optional[str] = None

    @property
    def combined(self) -> str:
        """Newline-separated context string for ASR providers.

        VibeVoice training data uses newline-separated ``customized_context``
        items (proper nouns, domain terms, full contextual sentences).
        Each comma-separated hot word / jargon term becomes its own line.
        """
        items: list[str] = []
        for source in [self.hot_words, self.user_jargon]:
            if not source or not source.strip():
                continue
            for token in source.split(","):
                token = token.strip()
                if token:
                    items.append(token)
        return "\n".join(items)

    def to_metadata(self) -> dict:
        """Return a dict suitable for Langfuse span metadata."""
        return {
            "hot_words": self.hot_words[:200] if self.hot_words else "",
            "user_jargon": self.user_jargon[:200] if self.user_jargon else "",
            "user_id": self.user_id,
            "combined_length": len(self.combined),
        }


async def gather_transcription_context(
    user_id: Optional[str] = None,
) -> TranscriptionContext:
    """Build structured transcription context: static hot words + cached user jargon.

    Args:
        user_id: If provided, also look up per-user jargon from Redis.

    Returns:
        TranscriptionContext with individual components.
    """
    from advanced_omi_backend.prompt_registry import get_prompt_registry

    registry = get_prompt_registry()
    try:
        hot_words = await registry.get_prompt("asr.hot_words")
    except Exception:
        logger.debug("Failed to fetch asr.hot_words prompt, using empty default")
        hot_words = ""

    user_jargon = ""
    if user_id:
        try:
            redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
            try:
                user_jargon = await redis_client.get(f"asr:jargon:{user_id}") or ""
            finally:
                await redis_client.close()
        except Exception:
            pass  # Redis unavailable → skip dynamic jargon

    return TranscriptionContext(
        hot_words=hot_words or "",
        user_jargon=user_jargon,
        user_id=user_id,
    )


async def get_asr_context(user_id: Optional[str] = None) -> str:
    """Build combined ASR context string (backward-compatible alias).

    Args:
        user_id: If provided, also look up per-user jargon from Redis.

    Returns:
        Newline-separated context string for ASR providers.
    """
    ctx = await gather_transcription_context(user_id)
    return ctx.combined
