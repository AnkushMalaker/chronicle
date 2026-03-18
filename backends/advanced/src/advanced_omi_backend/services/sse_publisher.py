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
import os
import time

import redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Lazy-initialized sync Redis client (for RQ workers)
_sync_redis: redis.Redis | None = None


def _get_sync_redis() -> redis.Redis:
    """Get or create the sync Redis client."""
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _sync_redis


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
        message = json.dumps(
            {
                "event": event_type,
                "data": data,
                "timestamp": time.time(),
            }
        )
        r.publish(f"sse:{user_id}", message)
    except Exception:
        # SSE publishing is best-effort — never fail the calling job
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
