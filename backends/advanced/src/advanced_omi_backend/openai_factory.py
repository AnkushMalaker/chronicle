"""Centralized OpenAI client factory.

Single source of truth for creating OpenAI/AsyncOpenAI clients. All other
modules that need an OpenAI client should use this factory instead of
creating clients directly.

Clients are cached by (api_key, base_url, is_async) to avoid repeated
SSL context creation (~400ms per instantiation).

Tracing is handled by the OTEL instrumentor (see observability/otel_setup.py),
which auto-instruments all OpenAI calls at startup. No per-client wrapping needed.
"""

import logging

import openai

logger = logging.getLogger(__name__)

_client_cache: dict[tuple[str, str, bool], openai.OpenAI | openai.AsyncOpenAI] = {}


def create_openai_client(api_key: str, base_url: str, is_async: bool = False):
    """Get or create a cached OpenAI client.

    Clients are cached by (api_key, base_url, is_async). If the API key or
    base URL changes (e.g. config reload), a new client is created automatically.

    Args:
        api_key: OpenAI API key
        base_url: OpenAI API base URL
        is_async: Whether to return AsyncOpenAI or sync OpenAI client

    Returns:
        OpenAI or AsyncOpenAI client instance
    """
    cache_key = (api_key, base_url, is_async)
    client = _client_cache.get(cache_key)
    if client is not None:
        return client

    if is_async:
        client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        client = openai.OpenAI(api_key=api_key, base_url=base_url)

    _client_cache[cache_key] = client
    logger.info(
        f"Created {'async' if is_async else 'sync'} OpenAI client for {base_url}"
    )
    return client
