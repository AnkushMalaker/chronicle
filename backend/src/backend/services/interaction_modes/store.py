"""Redis persistence for interaction sessions."""

from __future__ import annotations

import json
import time
from typing import Optional

import redis.asyncio as redis

from .contracts import InteractionSession

SESSION_RETENTION_SECONDS = 24 * 60 * 60
PROCESSED_RETENTION_SECONDS = 24 * 60 * 60
DEADLINES_KEY = "interaction:deadlines"


def _active_key(user_id: str, client_id: str) -> str:
    return f"interaction:active:{user_id}:{client_id}"


def _session_key(interaction_id: str) -> str:
    return f"interaction:session:{interaction_id}"


def _processed_key(input_id: str) -> str:
    return f"interaction:processed:{input_id}"


def interaction_lock_key(interaction_id: str) -> str:
    return f"interaction:lock:{interaction_id}"


class InteractionStore:
    """Owns active pointers, session JSON, and timeout deadlines."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    @staticmethod
    def _decode(raw):
        return raw.decode() if isinstance(raw, bytes) else raw

    async def get(self, interaction_id: str) -> Optional[InteractionSession]:
        raw = await self.redis.get(_session_key(interaction_id))
        if raw is None:
            return None
        raw = self._decode(raw)
        return InteractionSession.from_dict(json.loads(raw))

    async def get_active(
        self, user_id: str, client_id: str, *, now: Optional[float] = None
    ) -> Optional[InteractionSession]:
        key = _active_key(user_id, client_id)
        raw_id = await self.redis.get(key)
        if raw_id is None:
            return None
        interaction_id = self._decode(raw_id)
        session = await self.get(interaction_id)
        if session is None or session.status != "active":
            await self.redis.delete(key)
            return None
        # Do not finalize expiry in this low-level lookup. The processor owns the
        # plugin end callback and user-facing timeout reply. Returning an overdue
        # active session lets either the deadline sweep or the next queued turn
        # perform that complete transition instead of silently deleting it here.
        return session

    async def create(self, session: InteractionSession) -> bool:
        """Claim the user/device active slot and persist ``session``.

        Returns False when another producer won the activation race.
        """
        ttl = max(1, int(session.hard_deadline - time.time()))
        active_key = _active_key(session.user_id, session.client_id)
        claimed = await self.redis.set(
            active_key, session.interaction_id, ex=ttl, nx=True
        )
        if not claimed:
            return False
        try:
            await self.save(session)
        except Exception:
            current = await self.redis.get(active_key)
            if self._decode(current) == session.interaction_id:
                await self.redis.delete(active_key)
            raise
        return True

    async def is_processed(self, input_id: str) -> bool:
        return bool(await self.redis.exists(_processed_key(input_id)))

    async def mark_processed(self, input_id: str) -> None:
        await self.redis.set(
            _processed_key(input_id), "1", ex=PROCESSED_RETENTION_SECONDS
        )

    async def save(
        self,
        session: InteractionSession,
        *,
        processed_input_id: Optional[str] = None,
    ) -> None:
        """Persist one transition and its input marker in one Redis transaction."""
        pipe = self.redis.pipeline(transaction=True)
        pipe.set(
            _session_key(session.interaction_id),
            json.dumps(session.to_dict(), separators=(",", ":")),
            ex=SESSION_RETENTION_SECONDS,
        )
        if session.status == "active":
            remaining = max(1, int(session.hard_deadline - time.time()))
            pipe.expire(_active_key(session.user_id, session.client_id), remaining)
            pipe.zadd(DEADLINES_KEY, {session.interaction_id: session.next_deadline})
        else:
            pipe.zrem(DEADLINES_KEY, session.interaction_id)
        if processed_input_id:
            pipe.set(
                _processed_key(processed_input_id),
                "1",
                ex=PROCESSED_RETENTION_SECONDS,
            )
        await pipe.execute()

    async def end(
        self,
        session: InteractionSession,
        *,
        reason: str,
        now: Optional[float] = None,
        processed_input_id: Optional[str] = None,
    ) -> InteractionSession:
        ended_at = now if now is not None else time.time()
        session.status = "ended"
        session.ended_at = ended_at
        session.end_reason = reason
        await self.save(session, processed_input_id=processed_input_id)
        active_key = _active_key(session.user_id, session.client_id)
        current = await self.redis.get(active_key)
        if self._decode(current) == session.interaction_id:
            await self.redis.delete(active_key)
        return session

    async def due_interaction_ids(
        self, *, now: Optional[float] = None, limit: int = 100
    ) -> list[str]:
        deadline = now if now is not None else time.time()
        raw_ids = await self.redis.zrangebyscore(
            DEADLINES_KEY, min="-inf", max=deadline, start=0, num=limit
        )
        return [self._decode(value) for value in raw_ids]
