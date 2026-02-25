"""Centralized OpenAI client factory.

Single source of truth for creating OpenAI/AsyncOpenAI clients. All other
modules that need an OpenAI client should use this factory instead of
creating clients directly.

Tracing is handled by the OTEL instrumentor (see observability/otel_setup.py),
which auto-instruments all OpenAI calls at startup. No per-client wrapping needed.
"""

import logging

import openai

logger = logging.getLogger(__name__)


def create_openai_client(api_key: str, base_url: str, is_async: bool = False):
    """Create an OpenAI client.

    Args:
        api_key: OpenAI API key
        base_url: OpenAI API base URL
        is_async: Whether to return AsyncOpenAI or sync OpenAI client

    Returns:
        OpenAI or AsyncOpenAI client instance
    """
    if is_async:
        return openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        return openai.OpenAI(api_key=api_key, base_url=base_url)
