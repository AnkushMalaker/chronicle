"""Presenting a Chronicle API key, and checking it before we rely on it."""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Cheapest authenticated endpoint that exists on every backend: it resolves the
# bearer credential to a user and returns nothing expensive.
_WHOAMI_PATH = "/users/me"


def auth_headers(api_key: str) -> dict[str, str]:
    """Authorization header for an API key.

    API keys travel on the same header as a JWT, which is what lets clients that
    only offer an "API key" field talk to Chronicle unmodified.
    """
    return {"Authorization": f"Bearer {api_key}"}


def check_credentials(
    api_key: str, backend_url: str, *, timeout: float = 10.0, verify: bool = True
) -> bool:
    """Verify the API key is accepted, synchronously.

    Worth doing at startup: without it a bad credential surfaces later as an
    opaque WebSocket close on every reconnect attempt, with nothing in the logs
    pointing at auth.
    """
    if not api_key:
        logger.error("CHRONICLE_API_KEY is not set")
        return False
    try:
        with httpx.Client(timeout=timeout, verify=verify) as client:
            resp = client.get(
                f"{backend_url.rstrip('/')}{_WHOAMI_PATH}", headers=auth_headers(api_key)
            )
    except httpx.HTTPError as e:
        logger.error("Auth error: %s", e)
        return False
    return _log_outcome(resp.status_code)


async def acheck_credentials(
    api_key: str, backend_url: str, *, timeout: float = 10.0, verify: bool = True
) -> bool:
    """Async twin of :func:`check_credentials`, for clients already on asyncio."""
    if not api_key:
        logger.error("CHRONICLE_API_KEY is not set")
        return False
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
            resp = await client.get(
                f"{backend_url.rstrip('/')}{_WHOAMI_PATH}", headers=auth_headers(api_key)
            )
    except httpx.HTTPError as e:
        logger.error("Auth error: %s", e)
        return False
    return _log_outcome(resp.status_code)


def _log_outcome(status_code: int) -> bool:
    if status_code == 200:
        logger.info("Auth OK")
        return True
    if status_code == 401:
        logger.error("Auth failed: API key rejected (revoked, expired, or mistyped)")
    else:
        logger.error("Auth failed: HTTP %d", status_code)
    return False


def bearer_query_param(api_key: str) -> str:
    """The ``token=`` query value for endpoints that cannot set headers.

    Chronicle's WebSocket and audio URLs accept the credential as a query
    parameter; the backend resolves API keys and JWTs through the same strategy,
    so a key works wherever a JWT did.
    """
    return api_key


def describe_key(api_key: Optional[str]) -> str:
    """Public, log-safe identity of a key: its prefix, never the secret."""
    if not api_key:
        return "(unset)"
    parts = api_key.split("_", 2)
    if len(parts) == 3:
        return f"{parts[0]}_{parts[1]}_…"
    return "(malformed)"
