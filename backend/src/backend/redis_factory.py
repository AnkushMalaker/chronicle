"""Central Redis client factory.

The one place that knows ``REDIS_URL`` and how to construct a correctly-configured
Redis client. Call sites must not sprinkle ``os.getenv("REDIS_URL")`` +
``from_url(...)`` with ad-hoc ``decode_responses`` settings — that inconsistency is
what forces downstream code (e.g. ``SessionStore``) to defensively tolerate both
bytes and str values.

Two axes:

- **async vs sync.** FastAPI handlers, services, and the ``@async_job`` RQ wrapper
  use async clients (``redis.asyncio``). RQ's own queue/worker connection and the
  sync SSE/plugin-event-log publishers use sync clients (``redis``).
- **decode_responses.** ``True`` yields ``str`` values — convenient for string and
  JSON keys. ``False`` yields ``bytes`` — required by RQ and by the binary audio
  streams. Choose per use-case; default ``False`` matches RQ and the audio path.

Ownership: ``create_*`` returns a *fresh* client the caller owns and MUST close
(``await client.aclose()`` / ``client.close()``). This is the right choice for RQ
jobs (each runs in its own short-lived event loop), per-request handlers, and
per-connection pub/sub. Long-lived singletons (client manager, SSE publisher,
plugin router) create one client via ``create_*`` and hold it for the process
lifetime — there is intentionally no shared cache here, because an async client is
bound to the event loop that first uses it and caching one across RQ's per-job
loops would break.

This module is also the natural home for future cross-cutting concerns: connection
pool tuning, ``health_check_interval``, socket keepalive, and retry policy.
"""

import os

import redis
import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def create_async_redis(*, decode_responses: bool = False) -> aioredis.Redis:
    """Create a fresh async Redis client. The caller owns it and must ``aclose()``.

    Use for RQ jobs, per-request handlers, per-connection pub/sub, and the
    ``asyncio.run`` entrypoints of standalone async workers.
    """
    return aioredis.from_url(
        REDIS_URL, encoding="utf-8", decode_responses=decode_responses
    )


def create_sync_redis(*, decode_responses: bool = False) -> redis.Redis:
    """Create a fresh sync Redis client. The caller owns it and must ``close()``.

    Use for RQ queue/worker connections (``decode_responses=False``) and the sync
    SSE / plugin-event-log publishers.
    """
    return redis.from_url(
        REDIS_URL, encoding="utf-8", decode_responses=decode_responses
    )
