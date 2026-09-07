"""
SSE Event Publisher - publishes events to Redis pub/sub for Server-Sent Events.

This module provides both sync and async interfaces for publishing SSE events.
- Sync: Used by RQ workers (separate processes)
- Async: Used by FastAPI handlers

Events are published to per-user Redis pub/sub channels (sse:{user_id}).
The SSE endpoint subscribes to the user's channel and streams events to the browser.
"""

import json
import logging
import time

import redis
import redis.asyncio as aioredis

from backend.redis_factory import create_async_redis, create_sync_redis

logger = logging.getLogger(__name__)

# Lazy-initialized sync Redis client (for RQ workers)
_sync_redis: redis.Redis | None = None

# Lazy-initialized async client, for callers already on an event loop.
_async_redis: aioredis.Redis | None = None


def _get_sync_redis() -> redis.Redis:
    """Get or create the sync Redis client."""
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = create_sync_redis(decode_responses=True)
    return _sync_redis


def _get_async_redis() -> aioredis.Redis:
    """Get or create the async Redis client. Must be called from the loop."""
    global _async_redis
    if _async_redis is None:
        _async_redis = create_async_redis(decode_responses=True)
    return _async_redis


def _message(event_type: str, data: dict) -> str:
    return json.dumps({"event": event_type, "data": data, "timestamp": time.time()})


def publish_sse_event(user_id: str, event_type: str, data: dict) -> None:
    """
    Publish an SSE event to the user's channel (sync, for RQ workers).

    Args:
        user_id: The user's ID (MongoDB ObjectId string)
        event_type: Event name (e.g., 'conversation.created', 'job.completed')
        data: Event payload dict
    """
    try:
        r = _get_sync_redis()
        r.publish(f"sse:{user_id}", _message(event_type, data))
    except Exception:
        # SSE publishing is best-effort — never fail the calling job
        logger.debug("Failed to publish SSE event %s", event_type, exc_info=True)


async def publish_sse_event_async(user_id: str, event_type: str, data: dict) -> None:
    """Publish an SSE event from a caller already on the event loop.

    The sync client above must not be used there. Its first command on a dropped
    connection reconnects inline — a blocking ``getaddrinfo`` and ``connect`` on
    the loop thread, measured here at 3-5s per reconnect, during which nothing
    else in the process runs.
    """
    try:
        r = _get_async_redis()
        await r.publish(f"sse:{user_id}", _message(event_type, data))
    except Exception:
        # SSE publishing is best-effort — never fail the calling handler
        logger.debug("Failed to publish SSE event %s", event_type, exc_info=True)


# Throttle state for publish_sse_event_throttled
_last_publish: dict[str, float] = {}


def publish_sse_event_throttled(
    user_id: str, event_type: str, data: dict, min_interval: float = 1.0
) -> None:
    """
    Publish an SSE event, but skip if the same key was published within min_interval seconds.

    Used for high-frequency progress updates (e.g., job.progress every ~1s) to avoid
    hammering the frontend with too many invalidations.
    """
    key = f"{user_id}:{event_type}:{data.get('conversation_id', '')}"
    now = time.time()
    if now - _last_publish.get(key, 0) < min_interval:
        return
    _last_publish[key] = now
    publish_sse_event(user_id, event_type, data)
