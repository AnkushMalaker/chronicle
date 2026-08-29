import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.audio_contract.v2.codec import parse_media_envelope
from advanced_omi_backend.controllers.audio_v2_controller import _subscribe_v2_downlink
from advanced_omi_backend.services.response_coordinator import ResponseCoordinator
from advanced_omi_backend.services.voice_sessions import (
    VoiceCapabilities,
    VoiceSessionCoordinator,
)


async def test_typed_downlink_forwards_offer_then_atomic_opus_media():
    redis = fake_aioredis.FakeRedis(decode_responses=False)
    voices = VoiceSessionCoordinator(redis)
    started = await voices.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="capture-1",
        capture_epoch=3,
        socket_id="socket-1",
        advertised_protocol=2,
    )
    voice = await voices.ready(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="capture-1",
        capture_epoch=3,
        socket_id="socket-1",
        capabilities=VoiceCapabilities(
            mode="duplex_full",
            input_route="built_in_mic",
            output_route="speakerphone",
            native_sample_rate=48_000,
            aec={"requested": True, "available": True, "enabled": True},
            noise_suppression={"requested": True, "available": True, "enabled": True},
            fallback_reason=None,
        ),
    )
    responses = ResponseCoordinator(redis, voices)
    generation = await responses.begin_turn("user-1", "client-1")
    response = await responses.queue(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="capture-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        turn_id="turn-1",
        turn_revision=0,
        generation=generation,
        kind="speech",
        barge_in_allowed=True,
        trace_id="trace-1",
        causation_id="turn-1",
    )
    await responses.mark_ready(
        response.response_id, byte_length=4, duration_ms=20, sample_rate=24_000
    )
    websocket = SimpleNamespace(send_text=AsyncMock(), send_bytes=AsyncMock())
    task = asyncio.create_task(
        _subscribe_v2_downlink(
            websocket=websocket,
            redis_client=redis,
            voice_sessions=voices,
            responses=responses,
            client_state=SimpleNamespace(socket_id="socket-1"),
            user_id="user-1",
            client_id="client-1",
        )
    )
    await asyncio.sleep(0)

    await responses.offer(response.response_id, (b"opus",))
    for _ in range(20):
        if websocket.send_bytes.await_count:
            break
        await asyncio.sleep(0.01)

    assert websocket.send_text.await_count == 1
    assert websocket.send_bytes.await_count == 1
    envelope = parse_media_envelope(websocket.send_bytes.await_args.args[0])
    assert envelope.playback.opus_payload == b"opus"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
