"""One text/WAV delivery facade over coordinated phone and wearable transports."""

from __future__ import annotations

import io
import time
import uuid
import wave
from typing import TYPE_CHECKING, Awaitable, Callable

import redis.asyncio as redis

from advanced_omi_backend.redis_keys import ClientId, SessionId
from advanced_omi_backend.services.audio_stream.session_store import SessionStore
from advanced_omi_backend.services.response_coordinator import (
    ResponseCoordinator,
    ResponseRecord,
    StaleResponse,
)
from advanced_omi_backend.services.tts_client import synthesize_speech
from advanced_omi_backend.services.voice_sessions import VoiceSessionCoordinator

if TYPE_CHECKING:
    from advanced_omi_backend.services.wakeword.timing import WakeTimer

WavOperation = Callable[[], Awaitable[bytes]]


def _wav_metadata(wav: bytes) -> tuple[int, int]:
    try:
        with wave.open(io.BytesIO(wav), "rb") as reader:
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
    except (EOFError, wave.Error) as error:
        raise ValueError("coordinated response must be a valid WAV") from error
    if sample_rate <= 0 or frame_count <= 0:
        raise ValueError("coordinated response WAV must contain audio frames")
    duration_ms = max(1, round(frame_count * 1000 / sample_rate))
    return sample_rate, duration_ms


async def deliver_wav_response(
    redis_client: redis.Redis,
    client_id: ClientId,
    session_id: SessionId,
    operation: WavOperation,
    *,
    kind: str,
    generation: int | None = None,
    turn_id: str | None = None,
    turn_revision: int = 0,
    barge_in_allowed: bool = True,
    timer: WakeTimer | None = None,
) -> ResponseRecord:
    """Fence production, routing, and delivery against one authenticated target."""

    if not isinstance(client_id, ClientId):
        raise TypeError("deliver_wav_response requires ClientId")
    if not isinstance(session_id, SessionId):
        raise TypeError("deliver_wav_response requires SessionId")
    view = await SessionStore(redis_client).read(str(session_id))
    if (
        view is None
        or view.client_id != str(client_id)
        or not view.user_id
        or not view.connection_id
    ):
        raise StaleResponse("response target is not an authenticated audio session")

    voice_sessions = VoiceSessionCoordinator(redis_client)
    coordinator = ResponseCoordinator(redis_client, voice_sessions)
    if generation is None:
        generation = await coordinator.begin_turn(
            view.user_id,
            view.client_id,
            reason="replacement",
        )
    response_turn_id = turn_id or str(uuid.uuid4())
    trace_id = str(uuid.uuid4())

    if view.voice_session_id:
        voice = await voice_sessions.get(view.voice_session_id)
        if voice is None:
            raise StaleResponse("protocol-v1 response has no voice session")
        response = await coordinator.queue(
            user_id=view.user_id,
            client_id=view.client_id,
            audio_session_id=view.session_id,
            voice_session_id=view.voice_session_id,
            capture_epoch=view.capture_epoch,
            socket_id=view.connection_id,
            turn_id=response_turn_id,
            turn_revision=turn_revision,
            generation=generation,
            kind=kind,
            barge_in_allowed=barge_in_allowed if kind == "speech" else False,
            trace_id=trace_id,
            causation_id=response_turn_id,
        )
    else:
        response = await coordinator.queue_adapter(
            user_id=view.user_id,
            client_id=view.client_id,
            audio_session_id=view.session_id,
            turn_id=response_turn_id,
            turn_revision=turn_revision,
            generation=generation,
            kind=kind,
            trace_id=trace_id,
            causation_id=response_turn_id,
        )

    started = time.perf_counter()
    try:
        wav = await coordinator.synthesize(response.response_id, operation)
        if timer is not None:
            timer.tts_ms = (time.perf_counter() - started) * 1000
        sample_rate, duration_ms = _wav_metadata(wav)
        await coordinator.mark_ready(
            response.response_id,
            byte_length=len(wav),
            duration_ms=duration_ms,
            sample_rate=sample_rate,
        )
        if response.transport == "voice_v1":
            delivered = await coordinator.offer(response.response_id, wav)
        else:
            delivered = await coordinator.offer_adapter(response.response_id, wav)
        if timer is not None:
            timer.est_play_secs = duration_ms / 1000
            timer.mark_downlink()
        return delivered
    except Exception as error:
        try:
            await coordinator.fail(response.response_id, type(error).__name__)
        except StaleResponse:
            pass
        raise


async def deliver_text_response(
    redis_client: redis.Redis,
    client_id: ClientId,
    session_id: SessionId,
    text: str,
    *,
    generation: int | None = None,
    turn_id: str | None = None,
    turn_revision: int = 0,
    timer: WakeTimer | None = None,
) -> ResponseRecord | None:
    if not text:
        return None

    async def synthesize() -> bytes:
        return await synthesize_speech(text)

    return await deliver_wav_response(
        redis_client,
        client_id,
        session_id,
        synthesize,
        kind="speech",
        generation=generation,
        turn_id=turn_id,
        turn_revision=turn_revision,
        barge_in_allowed=True,
        timer=timer,
    )
