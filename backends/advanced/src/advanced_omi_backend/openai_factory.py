"""Centralized OpenAI client factory with optional LangFuse tracing.

Single source of truth for creating OpenAI/AsyncOpenAI clients. All other
modules that need an OpenAI client should use this factory instead of
duplicating LangFuse detection logic.
"""

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def is_langfuse_enabled() -> bool:
    """Check if LangFuse is properly configured (cached)."""
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
        and os.getenv("LANGFUSE_HOST")
    )


def create_openai_client(api_key: str, base_url: str, is_async: bool = False):
    """Create an OpenAI client with optional LangFuse tracing.

    Args:
        api_key: OpenAI API key
        base_url: OpenAI API base URL
        is_async: Whether to return AsyncOpenAI or sync OpenAI client

    Returns:
        OpenAI or AsyncOpenAI client instance (with or without LangFuse wrapping)
    """
    if is_langfuse_enabled():
        import langfuse.openai as openai_module

        logger.debug("Creating OpenAI client with LangFuse tracing")
    else:
        import openai as openai_module

        logger.debug("Creating OpenAI client without tracing")

    if is_async:
        return openai_module.AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        return openai_module.OpenAI(api_key=api_key, base_url=base_url)
