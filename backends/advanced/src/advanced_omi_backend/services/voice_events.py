"""Idempotency claims for authenticated protocol-v1 client events."""

from __future__ import annotations

import uuid

import redis.asyncio as redis

EVENT_RETENTION_SECONDS = 24 * 60 * 60


class VoiceEventDeduplicator:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def claim(self, *, user_id: str, client_id: str, event_id: uuid.UUID) -> bool:
        if not user_id or not client_id:
            raise ValueError("voice event requires authenticated user and client")
        key = f"voice:event:{user_id}:{client_id}:{event_id}"
        return bool(
            await self.redis.set(
                key,
                "1",
                ex=EVENT_RETENTION_SECONDS,
                nx=True,
            )
        )
