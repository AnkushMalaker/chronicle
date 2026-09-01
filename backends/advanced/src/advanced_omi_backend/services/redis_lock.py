"""Small async interface for cross-process single-flight claims."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.exceptions import LockError

from advanced_omi_backend.redis_factory import create_async_redis


class LockUnavailable(RuntimeError):
    pass


@asynccontextmanager
async def distributed_lock(
    key: str, *, timeout: int = 120, blocking_timeout: int = 30
) -> AsyncIterator[None]:
    """Hold one Redis-backed lock without leaking a client across event loops."""

    client = create_async_redis(decode_responses=True)
    lock = client.lock(key, timeout=timeout, blocking_timeout=blocking_timeout)
    acquired = False
    try:
        acquired = bool(await lock.acquire())
        if not acquired:
            raise LockUnavailable(f"could not acquire single-flight claim: {key}")
        yield
    finally:
        if acquired:
            try:
                await lock.release()
            except LockError:
                # The protected operation already completed or raised. An expired
                # lease must not hide that result behind a cleanup-only exception.
                pass
        await client.aclose()
