"""Single Redis-backed delivery path for interactive speech and tones."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal, TypeVar

import redis.asyncio as redis
from redis.exceptions import WatchError

from advanced_omi_backend.redis_keys import (
    ClientId,
    UserId,
    current_response,
    device_downlink_channel,
    response_generation,
    voice_response,
    voice_response_media,
)
from advanced_omi_backend.services.audio_stream.session_store import SessionStore
from advanced_omi_backend.services.voice_sessions import VoiceSessionCoordinator
from advanced_omi_backend.voice_protocol import (
    MAX_RESPONSE_BYTES,
    MAX_RESPONSE_DURATION_MS,
    ResponseAudio,
    ResponseCancel,
)

RESPONSE_RETENTION_SECONDS = 24 * 60 * 60
MEDIA_RETENTION_SECONDS = 5 * 60
GENERATION_RETENTION_SECONDS = 24 * 60 * 60
PLAYBACK_START_ACK_SECONDS = 5.0
PLAYBACK_COMPLETION_GRACE_SECONDS = 2.0

ResponseState = Literal[
    "queued",
    "synthesizing",
    "ready",
    "offered",
    "playing",
    "done",
    "cancelled",
    "failed",
]
ResponseKind = Literal["speech", "tone"]
ResponseTransport = Literal["voice_v1", "device_adapter"]
T = TypeVar("T")


class ResponseCoordinatorError(RuntimeError):
    """Base error for coordinated response delivery."""


class StaleResponse(ResponseCoordinatorError):
    """Async work or playback belongs to a superseded output generation."""


class InvalidResponseTransition(ResponseCoordinatorError):
    """A response acknowledgment is illegal from the stored state."""


@dataclass(frozen=True)
class ResponseRecord:
    response_id: str
    user_id: str
    client_id: str
    audio_session_id: str
    voice_session_id: str
    capture_epoch: int
    socket_id: str
    turn_id: str
    turn_revision: int
    generation: int
    kind: ResponseKind
    transport: ResponseTransport
    barge_in_allowed: bool
    trace_id: str
    causation_id: str
    state: ResponseState
    created_at: float
    updated_at: float
    byte_length: int | None = None
    duration_ms: int | None = None
    sample_rate: int | None = None
    playback_monotonic_ms: int | None = None
    terminal_reason: str | None = None


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


def _decode_hash(raw: dict) -> dict[str, str]:
    return {_decode(key): _decode(value) for key, value in raw.items()}


def _optional_int(values: dict[str, str], key: str) -> int | None:
    value = values.get(key, "")
    return int(value) if value else None


def _record_from_hash(raw: dict) -> ResponseRecord | None:
    if not raw:
        return None
    values = _decode_hash(raw)
    return ResponseRecord(
        response_id=values["response_id"],
        user_id=values["user_id"],
        client_id=values["client_id"],
        audio_session_id=values["audio_session_id"],
        voice_session_id=values["voice_session_id"],
        capture_epoch=int(values["capture_epoch"]),
        socket_id=values["socket_id"],
        turn_id=values["turn_id"],
        turn_revision=int(values["turn_revision"]),
        generation=int(values["generation"]),
        kind=values["kind"],
        transport=values["transport"],
        barge_in_allowed=values["barge_in_allowed"] == "1",
        trace_id=values["trace_id"],
        causation_id=values["causation_id"],
        state=values["state"],
        created_at=float(values["created_at"]),
        updated_at=float(values["updated_at"]),
        byte_length=_optional_int(values, "byte_length"),
        duration_ms=_optional_int(values, "duration_ms"),
        sample_rate=_optional_int(values, "sample_rate"),
        playback_monotonic_ms=_optional_int(values, "playback_monotonic_ms"),
        terminal_reason=values.get("terminal_reason") or None,
    )


def _record_mapping(record: ResponseRecord) -> dict[str, str]:
    return {
        "response_id": record.response_id,
        "user_id": record.user_id,
        "client_id": record.client_id,
        "audio_session_id": record.audio_session_id,
        "voice_session_id": record.voice_session_id,
        "capture_epoch": str(record.capture_epoch),
        "socket_id": record.socket_id,
        "turn_id": record.turn_id,
        "turn_revision": str(record.turn_revision),
        "generation": str(record.generation),
        "kind": record.kind,
        "transport": record.transport,
        "barge_in_allowed": "1" if record.barge_in_allowed else "0",
        "trace_id": record.trace_id,
        "causation_id": record.causation_id,
        "state": record.state,
        "created_at": str(record.created_at),
        "updated_at": str(record.updated_at),
        "byte_length": str(record.byte_length or ""),
        "duration_ms": str(record.duration_ms or ""),
        "sample_rate": str(record.sample_rate or ""),
        "playback_monotonic_ms": str(record.playback_monotonic_ms or ""),
        "terminal_reason": record.terminal_reason or "",
    }


class ResponseCoordinator:
    """Fence LLM/TTS/downlink/playback work with one client-wide generation."""

    def __init__(
        self,
        redis_client: redis.Redis,
        voice_sessions: VoiceSessionCoordinator,
    ):
        self.redis = redis_client
        self.voice_sessions = voice_sessions

    @staticmethod
    def _generation_key(user_id: str, client_id: str) -> str:
        return response_generation(
            UserId.from_value(user_id), ClientId.from_value(client_id)
        )

    @staticmethod
    def _current_key(user_id: str, client_id: str) -> str:
        return current_response(
            UserId.from_value(user_id), ClientId.from_value(client_id)
        )

    async def get(self, response_id: str) -> ResponseRecord | None:
        return _record_from_hash(await self.redis.hgetall(voice_response(response_id)))

    async def current_generation(self, user_id: str, client_id: str) -> int:
        raw = await self.redis.get(self._generation_key(user_id, client_id))
        return int(_decode(raw) or 0)

    async def assert_generation(
        self, user_id: str, client_id: str, generation: int
    ) -> None:
        if await self.current_generation(user_id, client_id) != generation:
            raise StaleResponse("turn generation was superseded")

    async def begin_turn(
        self, user_id: str, client_id: str, *, reason: str = "new_turn"
    ) -> int:
        """Supersede every older async/output result and return the new generation."""

        generation_key = self._generation_key(user_id, client_id)
        current_key = self._current_key(user_id, client_id)
        cancelled: ResponseRecord | None = None
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(generation_key, current_key)
                    generation = int(_decode(await pipe.get(generation_key)) or 0) + 1
                    current_id = _decode(await pipe.get(current_key))
                    current_record_key = (
                        voice_response(current_id) if current_id else None
                    )
                    if current_record_key:
                        await pipe.watch(current_record_key)
                        if _decode(await pipe.get(current_key)) != current_id:
                            await pipe.unwatch()
                            continue
                        current = _record_from_hash(
                            await pipe.hgetall(current_record_key)
                        )
                    else:
                        current = None
                    now = time.time()
                    pipe.multi()
                    pipe.set(
                        generation_key,
                        generation,
                        ex=GENERATION_RETENTION_SECONDS,
                    )
                    pipe.delete(current_key)
                    if current is not None and current.state not in {
                        "done",
                        "cancelled",
                        "failed",
                    }:
                        cancelled = ResponseRecord(
                            **{
                                **current.__dict__,
                                "state": "cancelled",
                                "updated_at": now,
                                "terminal_reason": reason,
                            }
                        )
                        pipe.hset(
                            current_record_key,
                            mapping={
                                "state": "cancelled",
                                "updated_at": str(now),
                                "terminal_reason": reason,
                            },
                        )
                        pipe.expire(current_record_key, RESPONSE_RETENTION_SECONDS)
                    await pipe.execute()
                    break
                except WatchError:
                    cancelled = None
                    continue

        if cancelled is not None and cancelled.state == "cancelled":
            event = ResponseCancel(
                type="response.cancel",
                event_id=uuid.uuid4(),
                client_id=cancelled.client_id,
                audio_session_id=cancelled.audio_session_id,
                voice_session_id=cancelled.voice_session_id,
                capture_epoch=cancelled.capture_epoch,
                sent_at=datetime.now(timezone.utc),
                response_id=cancelled.response_id,
                generation=generation,
                reason=(
                    reason
                    if reason
                    in {
                        "barge_in",
                        "new_turn",
                        "replacement",
                        "route_change",
                        "disconnect",
                        "session_stopped",
                    }
                    else "replacement"
                ),
            )
            await self._publish(cancelled.client_id, event.model_dump(mode="json"))
        return generation

    async def queue(
        self,
        *,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        voice_session_id: str,
        capture_epoch: int,
        socket_id: str,
        turn_id: str,
        turn_revision: int,
        generation: int,
        kind: ResponseKind,
        barge_in_allowed: bool,
        trace_id: str,
        causation_id: str,
    ) -> ResponseRecord:
        if kind == "tone" and barge_in_allowed:
            raise ValueError("tones cannot claim barge-in support")
        if not await self.voice_sessions.binding_matches(
            user_id=user_id,
            client_id=client_id,
            audio_session_id=audio_session_id,
            voice_session_id=voice_session_id,
            capture_epoch=capture_epoch,
            socket_id=socket_id,
        ):
            raise StaleResponse("response target is not the active ready voice session")

        generation_key = self._generation_key(user_id, client_id)
        current_key = self._current_key(user_id, client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(generation_key, current_key)
                    stored_generation = int(
                        _decode(await pipe.get(generation_key)) or 0
                    )
                    if stored_generation != generation:
                        raise StaleResponse("response generation was superseded")
                    if await pipe.get(current_key) is not None:
                        raise InvalidResponseTransition(
                            "a response is already current; supersede it first"
                        )
                    now = time.time()
                    record = ResponseRecord(
                        response_id=str(uuid.uuid4()),
                        user_id=user_id,
                        client_id=client_id,
                        audio_session_id=audio_session_id,
                        voice_session_id=voice_session_id,
                        capture_epoch=capture_epoch,
                        socket_id=socket_id,
                        turn_id=turn_id,
                        turn_revision=turn_revision,
                        generation=generation,
                        kind=kind,
                        transport="voice_v1",
                        barge_in_allowed=barge_in_allowed,
                        trace_id=trace_id,
                        causation_id=causation_id,
                        state="queued",
                        created_at=now,
                        updated_at=now,
                    )
                    record_key = voice_response(record.response_id)
                    pipe.multi()
                    pipe.hset(record_key, mapping=_record_mapping(record))
                    pipe.expire(record_key, RESPONSE_RETENTION_SECONDS)
                    pipe.set(
                        current_key,
                        record.response_id,
                        ex=RESPONSE_RETENTION_SECONDS,
                    )
                    await pipe.execute()
                    break
                except WatchError:
                    continue

        await self.assert_current(record)
        return record

    async def queue_adapter(
        self,
        *,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        turn_id: str,
        turn_revision: int,
        generation: int,
        kind: ResponseKind,
        trace_id: str,
        causation_id: str,
    ) -> ResponseRecord:
        """Queue a wearable response through an authenticated capture adapter."""

        view = await SessionStore(self.redis).read(audio_session_id)
        if (
            view is None
            or view.user_id != user_id
            or view.client_id != client_id
            or view.voice_session_id
            or not view.connection_id
            or not view.websocket_connected
        ):
            raise StaleResponse(
                "device adapter target is not an active capture binding"
            )

        generation_key = self._generation_key(user_id, client_id)
        current_key = self._current_key(user_id, client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(generation_key, current_key)
                    stored_generation = int(
                        _decode(await pipe.get(generation_key)) or 0
                    )
                    if stored_generation != generation:
                        raise StaleResponse("response generation was superseded")
                    if await pipe.get(current_key) is not None:
                        raise InvalidResponseTransition(
                            "a response is already current; supersede it first"
                        )
                    now = time.time()
                    record = ResponseRecord(
                        response_id=str(uuid.uuid4()),
                        user_id=user_id,
                        client_id=client_id,
                        audio_session_id=audio_session_id,
                        voice_session_id="",
                        capture_epoch=view.capture_epoch,
                        socket_id=view.connection_id,
                        turn_id=turn_id,
                        turn_revision=turn_revision,
                        generation=generation,
                        kind=kind,
                        transport="device_adapter",
                        barge_in_allowed=False,
                        trace_id=trace_id,
                        causation_id=causation_id,
                        state="queued",
                        created_at=now,
                        updated_at=now,
                    )
                    record_key = voice_response(record.response_id)
                    pipe.multi()
                    pipe.hset(record_key, mapping=_record_mapping(record))
                    pipe.expire(record_key, RESPONSE_RETENTION_SECONDS)
                    pipe.set(
                        current_key,
                        record.response_id,
                        ex=RESPONSE_RETENTION_SECONDS,
                    )
                    await pipe.execute()
                    break
                except WatchError:
                    continue

        await self.assert_current(record)
        return record

    async def assert_current(self, record: ResponseRecord) -> None:
        generation = await self.current_generation(record.user_id, record.client_id)
        current_id = _decode(
            await self.redis.get(self._current_key(record.user_id, record.client_id))
        )
        if generation != record.generation or current_id != record.response_id:
            raise StaleResponse("response is not current")

    async def _set_state(
        self,
        response_id: str,
        *,
        expected: set[ResponseState],
        state: ResponseState,
        updates: dict[str, str] | None = None,
    ) -> ResponseRecord:
        record_key = voice_response(response_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(record_key)
                    record = _record_from_hash(await pipe.hgetall(record_key))
                    if record is None:
                        raise StaleResponse("response does not exist")
                    generation_key = self._generation_key(
                        record.user_id, record.client_id
                    )
                    current_key = self._current_key(record.user_id, record.client_id)
                    await pipe.watch(generation_key, current_key)
                    if (
                        int(_decode(await pipe.get(generation_key)) or 0)
                        != record.generation
                        or _decode(await pipe.get(current_key)) != response_id
                    ):
                        raise StaleResponse("response generation was superseded")
                    if record.state not in expected:
                        raise InvalidResponseTransition(
                            f"cannot transition {record.state} to {state}"
                        )
                    now = time.time()
                    mapping = {"state": state, "updated_at": str(now)}
                    mapping.update(updates or {})
                    pipe.multi()
                    pipe.hset(record_key, mapping=mapping)
                    pipe.expire(record_key, RESPONSE_RETENTION_SECONDS)
                    if state in {"done", "cancelled", "failed"}:
                        pipe.delete(current_key)
                    await pipe.execute()
                    updated = await self.get(response_id)
                    if updated is None:
                        raise StaleResponse("response disappeared")
                    return updated
                except WatchError:
                    continue

    async def synthesize(
        self,
        response_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        synthesizing = await self._set_state(
            response_id,
            expected={"queued"},
            state="synthesizing",
        )
        result = await operation()
        await self.assert_current(synthesizing)
        return result

    async def fail(self, response_id: str, reason: str) -> ResponseRecord:
        return await self._set_state(
            response_id,
            expected={"queued", "synthesizing", "ready", "offered", "playing"},
            state="failed",
            updates={"terminal_reason": reason},
        )

    async def expire_stalled(
        self, response_id: str, *, now: float | None = None
    ) -> ResponseRecord:
        """Fail an offered/playing response whose native ACK deadline elapsed."""

        record = await self.get(response_id)
        if record is None:
            raise StaleResponse("response does not exist")
        current_time = now if now is not None else time.time()
        age_seconds = current_time - record.updated_at
        if record.state == "offered" and age_seconds >= PLAYBACK_START_ACK_SECONDS:
            return await self.fail(response_id, "playback_start_ack_timeout")
        expected_play_seconds = (record.duration_ms or 0) / 1000
        if (
            record.state == "playing"
            and age_seconds >= expected_play_seconds + PLAYBACK_COMPLETION_GRACE_SECONDS
        ):
            return await self.fail(response_id, "playback_completion_ack_timeout")
        return record

    async def health(self, user_id: str, client_id: str) -> dict:
        current_id = _decode(
            await self.redis.get(self._current_key(user_id, client_id))
        )
        current = await self.get(current_id) if current_id else None
        return {
            "generation": await self.current_generation(user_id, client_id),
            "current_response_id": current_id,
            "current_state": current.state if current else None,
            "current_updated_at": current.updated_at if current else None,
        }

    async def mark_ready(
        self,
        response_id: str,
        *,
        byte_length: int,
        duration_ms: int,
        sample_rate: int,
    ) -> ResponseRecord:
        if byte_length <= 0 or byte_length > MAX_RESPONSE_BYTES:
            raise ValueError("response WAV exceeds byte limit")
        if duration_ms <= 0 or duration_ms > MAX_RESPONSE_DURATION_MS:
            raise ValueError("response WAV exceeds duration limit")
        if sample_rate <= 0:
            raise ValueError("response sample rate must be positive")
        return await self._set_state(
            response_id,
            expected={"queued", "synthesizing"},
            state="ready",
            updates={
                "byte_length": str(byte_length),
                "duration_ms": str(duration_ms),
                "sample_rate": str(sample_rate),
            },
        )

    async def offer(self, response_id: str, wav: bytes) -> ResponseRecord:
        record = await self.get(response_id)
        if record is None or record.state != "ready" or record.transport != "voice_v1":
            raise InvalidResponseTransition("only a ready response can be offered")
        await self.assert_current(record)
        if (
            record.byte_length is None
            or len(wav) != record.byte_length
            or len(wav) > MAX_RESPONSE_BYTES
        ):
            raise ValueError("WAV body does not match response metadata")
        if not await self.voice_sessions.binding_matches(
            user_id=record.user_id,
            client_id=record.client_id,
            audio_session_id=record.audio_session_id,
            voice_session_id=record.voice_session_id,
            capture_epoch=record.capture_epoch,
            socket_id=record.socket_id,
        ):
            raise StaleResponse("voice binding became stale before offer")

        await self.redis.set(
            voice_response_media(response_id), wav, ex=MEDIA_RETENTION_SECONDS
        )
        await self.assert_current(record)
        if not await self.voice_sessions.binding_matches(
            user_id=record.user_id,
            client_id=record.client_id,
            audio_session_id=record.audio_session_id,
            voice_session_id=record.voice_session_id,
            capture_epoch=record.capture_epoch,
            socket_id=record.socket_id,
        ):
            raise StaleResponse("voice binding became stale while offering")
        offered = await self._set_state(
            response_id, expected={"ready"}, state="offered"
        )
        event = ResponseAudio(
            type="response.audio",
            event_id=uuid.uuid4(),
            client_id=offered.client_id,
            audio_session_id=offered.audio_session_id,
            voice_session_id=offered.voice_session_id,
            capture_epoch=offered.capture_epoch,
            sent_at=datetime.now(timezone.utc),
            turn_id=offered.turn_id,
            turn_revision=offered.turn_revision,
            response_id=offered.response_id,
            generation=offered.generation,
            sequence=0,
            kind=offered.kind,
            barge_in_allowed=offered.barge_in_allowed,
            media_type="audio/wav",
            sample_rate=offered.sample_rate,
            byte_length=offered.byte_length,
            duration_ms=offered.duration_ms,
            payload_length=offered.byte_length,
            trace_id=offered.trace_id,
            causation_id=offered.causation_id,
        )
        await self._publish(offered.client_id, event.model_dump(mode="json"))
        await self.assert_current(offered)
        return offered

    async def offer_adapter(self, response_id: str, wav: bytes) -> ResponseRecord:
        """Publish one bound legacy device frame and terminally settle it."""

        record = await self.get(response_id)
        if (
            record is None
            or record.state != "ready"
            or record.transport != "device_adapter"
        ):
            raise InvalidResponseTransition(
                "only a ready device-adapter response can be offered"
            )
        await self.assert_current(record)
        view = await SessionStore(self.redis).read(record.audio_session_id)
        if (
            view is None
            or view.user_id != record.user_id
            or view.client_id != record.client_id
            or view.connection_id != record.socket_id
            or view.capture_epoch != record.capture_epoch
            or view.voice_session_id
            or not view.websocket_connected
        ):
            raise StaleResponse("device adapter binding became stale before offer")
        if record.byte_length is None or len(wav) != record.byte_length:
            raise ValueError("WAV body does not match response metadata")

        offered = await self._set_state(
            response_id,
            expected={"ready"},
            state="offered",
        )
        payload = {
            "type": "play-audio",
            "data": {
                "audio_b64": base64.b64encode(wav).decode("ascii"),
                "format": "wav",
                "response_id": offered.response_id,
                "generation": offered.generation,
                "audio_session_id": offered.audio_session_id,
            },
        }
        await self._publish(offered.client_id, payload)
        await self.assert_current(offered)
        playing = await self._set_state(
            response_id,
            expected={"offered"},
            state="playing",
        )
        return await self._set_state(
            playing.response_id,
            expected={"playing"},
            state="done",
        )

    async def read_media(self, response_id: str) -> bytes | None:
        return await self.redis.get(voice_response_media(response_id))

    async def playback(
        self,
        *,
        response_id: str,
        generation: int,
        state: Literal["started", "done", "cancelled", "failed"],
        user_id: str,
        client_id: str,
        audio_session_id: str,
        voice_session_id: str,
        capture_epoch: int,
        socket_id: str,
        monotonic_timestamp_ms: int,
    ) -> ResponseRecord:
        record = await self.get(response_id)
        if record is None or record.generation != generation:
            raise StaleResponse("playback acknowledgment generation is stale")
        if (
            record.user_id != user_id
            or record.client_id != client_id
            or record.audio_session_id != audio_session_id
            or record.voice_session_id != voice_session_id
            or record.capture_epoch != capture_epoch
            or record.socket_id != socket_id
            or not await self.voice_sessions.binding_matches(
                user_id=user_id,
                client_id=client_id,
                audio_session_id=audio_session_id,
                voice_session_id=voice_session_id,
                capture_epoch=capture_epoch,
                socket_id=socket_id,
            )
        ):
            raise StaleResponse("playback acknowledgment binding is stale")

        transitions: dict[str, tuple[set[ResponseState], ResponseState]] = {
            "started": ({"offered"}, "playing"),
            "done": ({"playing"}, "done"),
            "cancelled": ({"offered", "playing"}, "cancelled"),
            "failed": ({"offered", "playing"}, "failed"),
        }
        expected, next_state = transitions[state]
        terminal_reason = state if state in {"cancelled", "failed"} else ""
        return await self._set_state(
            response_id,
            expected=expected,
            state=next_state,
            updates={
                "playback_monotonic_ms": str(monotonic_timestamp_ms),
                "terminal_reason": terminal_reason,
            },
        )

    async def _publish(self, client_id: str, payload: dict) -> None:
        channel = str(device_downlink_channel(ClientId.from_value(client_id)))
        await self.redis.publish(
            channel, json.dumps(payload, separators=(",", ":"), sort_keys=True)
        )
