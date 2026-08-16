import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.client import ClientState
from advanced_omi_backend.controllers import websocket_controller
from advanced_omi_backend.redis_keys import ClientId, device_downlink_channel
from advanced_omi_backend.services.response_coordinator import ResponseCoordinator
from advanced_omi_backend.services.voice_sessions import VoiceSessionCoordinator
from advanced_omi_backend.voice_protocol import VoiceCapabilities

pytestmark = pytest.mark.unit


def _capabilities() -> VoiceCapabilities:
    return VoiceCapabilities(
        mode="duplex_full",
        input_route="built_in_mic",
        output_route="speakerphone",
        native_sample_rate=48000,
        aec={"requested": True, "available": True, "enabled": True},
        noise_suppression={
            "requested": True,
            "available": True,
            "enabled": True,
        },
        fallback_reason=None,
    )


async def _runtime():
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    voice_sessions = VoiceSessionCoordinator(redis_client)
    started = await voice_sessions.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=2,
        socket_id="socket-1",
        advertised_protocol=1,
    )
    ready = await voice_sessions.ready(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=2,
        socket_id="socket-1",
        capabilities=_capabilities(),
    )
    responses = ResponseCoordinator(redis_client, voice_sessions)
    generation = await responses.begin_turn("user-1", "client-1")
    response = await responses.queue(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=ready.voice_session_id,
        capture_epoch=2,
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
        response.response_id, byte_length=12, duration_ms=250, sample_rate=24000
    )
    return redis_client, voice_sessions, responses, ready, response


async def test_real_downlink_forwards_header_then_binary_only_to_bound_socket():
    redis_client, voice_sessions, responses, ready, response = await _runtime()
    channel = str(device_downlink_channel(ClientId.from_value("client-1")))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    await pubsub.get_message(timeout=1)
    await responses.offer(response.response_id, b"RIFF12345678")
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    payload = json.loads(message["data"])
    websocket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock())

    forwarded = await websocket_controller._forward_interactive_downlink(
        websocket=websocket,
        redis_client=redis_client,
        voice_sessions=voice_sessions,
        payload=payload,
        user_id="user-1",
        client_id="client-1",
        socket_id="socket-1",
    )
    stale = await websocket_controller._forward_interactive_downlink(
        websocket=websocket,
        redis_client=redis_client,
        voice_sessions=voice_sessions,
        payload=payload,
        user_id="user-1",
        client_id="client-1",
        socket_id="stale-socket",
    )

    assert forwarded is True
    assert stale is False
    websocket.send_json.assert_awaited_once()
    websocket.send_bytes.assert_awaited_once_with(b"RIFF12345678")


async def test_phone_playback_event_enters_response_coordinator_through_handler():
    redis_client, _voice_sessions, responses, ready, response = await _runtime()
    await responses.offer(response.response_id, b"RIFF12345678")
    state = ClientState("client-1", "user-1")
    state.socket_id = "socket-1"
    producer = SimpleNamespace(redis_client=redis_client)
    event = {
        "type": "response.playback",
        "protocol": 1,
        "event_id": "00000000-0000-4000-8000-000000000021",
        "client_id": "client-1",
        "audio_session_id": "audio-1",
        "voice_session_id": ready.voice_session_id,
        "capture_epoch": 2,
        "sent_at": "2026-08-16T12:00:00Z",
        "response_id": response.response_id,
        "generation": response.generation,
        "state": "started",
        "monotonic_timestamp_ms": 10,
        "error_code": None,
    }

    result = await websocket_controller._handle_phone_voice_event(
        payload=event,
        client_state=state,
        audio_stream_producer=producer,
    )
    duplicate = await websocket_controller._handle_phone_voice_event(
        payload=event,
        client_state=state,
        audio_stream_producer=producer,
    )

    assert result.state == "playing"
    assert duplicate is None
    assert (await responses.get(response.response_id)).state == "playing"


async def test_old_phone_activation_returns_upgrade_boundary_without_ending_capture():
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    state = ClientState("client-1", "user-1")
    state.socket_id = "socket-1"
    state.stream_session_id = "audio-1"
    state.voice_duplex_protocol = None
    producer = SimpleNamespace(redis_client=redis_client)
    channel = str(device_downlink_channel(ClientId.from_value("client-1")))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    await pubsub.get_message(timeout=1)

    result = await websocket_controller.request_voice_session_start(
        client_state=state,
        audio_stream_producer=producer,
    )
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    payload = json.loads(message["data"])

    assert result is None
    assert payload["error"] == "client_upgrade_required"
    assert state.stream_session_id == "audio-1"
