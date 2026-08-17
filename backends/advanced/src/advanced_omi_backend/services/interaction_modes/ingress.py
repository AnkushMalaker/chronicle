"""Fast transcript ingress for active and newly activated interaction modes."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as redis

from .contracts import InteractionInput, InteractionSession, InteractionSource
from .registry import InteractionRegistry, normalize_interaction_text
from .store import InteractionStore

INPUT_STREAM = "interaction:inputs"
DEDUPE_SECONDS = 5


@dataclass(frozen=True)
class InteractionIngressResult:
    consumed: bool
    accepted: bool = False
    interaction_id: Optional[str] = None
    mode_id: Optional[str] = None
    reason: Optional[str] = None


class InteractionIngress:
    """Claims activations and enqueues turns without doing plugin or MCP work."""

    def __init__(self, redis_client: redis.Redis, registry: InteractionRegistry):
        self.redis = redis_client
        self.registry = registry
        self.store = InteractionStore(redis_client)

    async def submit(
        self,
        *,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        text: str,
        source: InteractionSource,
        muted: bool = False,
        now: Optional[float] = None,
    ) -> InteractionIngressResult:
        received_at = now if now is not None else time.time()
        text = (text or "").strip()
        if not text:
            return InteractionIngressResult(consumed=False, reason="empty")

        active = await self.store.get_active(user_id, client_id, now=received_at)
        match = self.registry.match(text) if active is None else None
        if active is None and match is None:
            return InteractionIngressResult(consumed=False, reason="no_mode")

        # Once a mode is active it owns this device's utterances.  Muted input is
        # therefore consumed (so it cannot leak to normal plugins) but not queued.
        if muted:
            return InteractionIngressResult(
                consumed=True,
                interaction_id=active.interaction_id if active else None,
                mode_id=active.mode_id if active else match.definition.mode_id,
                reason="tts_muted",
            )

        canonical = normalize_interaction_text(text)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        dedupe_key = f"interaction:dedupe:{user_id}:{client_id}:{digest}"
        claimed = await self.redis.set(dedupe_key, source, ex=DEDUPE_SECONDS, nx=True)
        prior_source = None if claimed else await self.redis.get(dedupe_key)
        if isinstance(prior_source, bytes):
            prior_source = prior_source.decode()
        # The same spoken audio can arrive once from streaming STT and once from
        # the acoustic wake path. Suppress that cross-source duplicate, while
        # still allowing a user to repeat a short command through one source.
        if not claimed and prior_source != source:
            return InteractionIngressResult(
                consumed=True,
                interaction_id=active.interaction_id if active else None,
                mode_id=active.mode_id if active else match.definition.mode_id,
                reason="duplicate",
            )

        activation_phrase: Optional[str] = None
        if active is None:
            interaction_id = str(uuid.uuid4())
            definition = match.definition
            active = InteractionSession(
                interaction_id=interaction_id,
                mode_id=definition.mode_id,
                owner_plugin_id=match.owner_plugin_id,
                user_id=user_id,
                client_id=client_id,
                audio_session_id=audio_session_id,
                phase="starting",
                plugin_state={},
                started_at=received_at,
                last_activity_at=received_at,
                idle_timeout_seconds=definition.idle_timeout_seconds,
                max_duration_seconds=definition.max_duration_seconds,
            )
            if not await self.store.create(active):
                active = await self.store.get_active(
                    user_id, client_id, now=received_at
                )
                if active is None:
                    return InteractionIngressResult(
                        consumed=True, reason="activation_race"
                    )
                kind = "turn"
                queued_text = text
            else:
                kind = "start"
                queued_text = match.remainder
                activation_phrase = match.activation_phrase
        else:
            kind = "turn"
            queued_text = text

        item = InteractionInput(
            input_id=str(uuid.uuid4()),
            interaction_id=active.interaction_id,
            kind=kind,
            user_id=user_id,
            client_id=client_id,
            audio_session_id=audio_session_id,
            text=queued_text,
            source=source,
            received_at=received_at,
            activation_phrase=activation_phrase,
        )
        await self.redis.xadd(
            INPUT_STREAM,
            {"input": json.dumps(item.to_dict(), separators=(",", ":"))},
        )
        return InteractionIngressResult(
            consumed=True,
            accepted=True,
            interaction_id=active.interaction_id,
            mode_id=active.mode_id,
            reason=kind,
        )
