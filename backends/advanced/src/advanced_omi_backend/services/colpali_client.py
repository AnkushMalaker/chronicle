"""Client for the Chronicle ColPali service (visual search over saved screenshots).

Resolved like every other GPU peer: env override, then minidisc/Tailscale discovery.
The service is additive — screenshot search works from the vault notes without it —
so every failure degrades to ``None`` or an empty result rather than raising.

Both a sync and an async client exist on purpose. The embed cron is async, while
``VaultTools.dispatch`` is synchronous and called from inside the memory agent's
loop, so search has to be callable without an event loop.
"""

import json
import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Search is a numpy dot product; anything slower means the node is unhealthy and the
# right answer is to fail fast to the text-search fallback rather than stall a chat.
SEARCH_TIMEOUT = float(os.getenv("COLPALI_SEARCH_TIMEOUT", "10"))
EMBED_TIMEOUT = float(os.getenv("COLPALI_EMBED_TIMEOUT", "120"))
HEALTH_TIMEOUT = float(os.getenv("COLPALI_HEALTH_TIMEOUT", "3"))

_DISCOVERY_TTL_SECS = 30.0
_cached: tuple[Optional[str], float] = (None, 0.0)


def resolve_colpali_url() -> Optional[str]:
    """Resolve the service base URL (env → discovery → None), cached briefly."""
    global _cached
    url = os.getenv("COLPALI_URL")
    if url:
        return url.rstrip("/")
    cached_url, cached_at = _cached
    if time.monotonic() - cached_at < _DISCOVERY_TTL_SECS:
        return cached_url
    resolved = None
    try:
        # Lazy import: sys.path-dependent optional module (repo-root `discovery`)
        from discovery import CHRONICLE_COLPALI, resolve_service_url

        resolved = resolve_service_url(None, CHRONICLE_COLPALI, default=None)
    except ImportError:
        resolved = None
    if resolved:
        resolved = resolved.rstrip("/")
    _cached = (resolved, time.monotonic())
    return resolved


async def health() -> Optional[dict[str, Any]]:
    """Probe the service. ``None`` means unreachable — asleep, or not deployed."""
    base_url = resolve_colpali_url()
    if not base_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            response = await client.get(f"{base_url}/health")
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        logger.debug("ColPali health probe failed: %s", exc)
        return None


async def embed_image(
    doc_id: str,
    user_id: str,
    data: bytes,
    content_type: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Index one image. Raises on failure so the caller can count the attempt."""
    base_url = resolve_colpali_url()
    if not base_url:
        raise RuntimeError("ColPali service is not configured")
    async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as client:
        response = await client.post(
            f"{base_url}/embed",
            files={"file": (f"{doc_id}.img", data, content_type)},
            data={
                "doc_id": doc_id,
                "user_id": user_id,
                "metadata": json.dumps(metadata, default=str),
            },
        )
        response.raise_for_status()
        return response.json()


async def indexed_documents(user_id: str) -> Optional[list[str]]:
    """Doc ids the service currently holds for a user, or ``None`` if unreachable."""
    base_url = resolve_colpali_url()
    if not base_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
            response = await client.get(
                f"{base_url}/documents", params={"user_id": user_id}
            )
            response.raise_for_status()
            return list(response.json().get("doc_ids") or [])
    except Exception as exc:
        logger.warning("ColPali document listing failed: %s", exc)
        return None


def search_images_sync(
    query: str, user_id: str, limit: int = 5
) -> Optional[list[dict[str, Any]]]:
    """Blocking visual search. ``None`` means unavailable, ``[]`` means no matches.

    Synchronous because the memory agent's tool dispatch is synchronous. The tight
    timeout above bounds how long that can block the event loop.
    """
    base_url = resolve_colpali_url()
    if not base_url:
        return None
    try:
        with httpx.Client(timeout=SEARCH_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/search",
                json={"query": query, "user_id": user_id, "limit": limit},
            )
            response.raise_for_status()
            return list(response.json().get("hits") or [])
    except Exception as exc:
        logger.warning("ColPali search failed: %s", exc)
        return None
