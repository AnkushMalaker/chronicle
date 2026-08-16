import asyncio
import io
import json
import wave
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.redis_keys import ClientId, SessionId, device_downlink_channel
from advanced_omi_backend.services import response_delivery
from advanced_omi_backend.services.audio_stream.session_store import SessionStore
from advanced_omi_backend.services.response_coordinator import (
    ResponseCoordinator,
    StaleResponse,
)
from advanced_omi_backend.services.response_delivery import deliver_text_response
from advanced_omi_backend.services.voice_sessions import VoiceSessionCoordinator
from advanced_omi_backend.voice_protocol import VoiceCapabilities

pytestmark = pytest.mark.unit


@pytest.fixture
def redis_client():
    return fake_aioredis.FakeRedis(decode_responses=False)


@pytest.fixture
def voice_coordinator(redis_client):
    return VoiceSessionCoordinator(redis_client)


@pytest.fixture
def coordinator(redis_client, voice_coordinator):
    return ResponseCoordinator(redis_client, voice_coordinator)


async def _ready_voice(voice_coordinator: VoiceSessionCoordinator):
    started = await voice_coordinator.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=3,
        socket_id="socket-1",
        advertised_protocol=1,
    )
    capabilities = VoiceCapabilities(
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
    return await voice_coordinator.ready(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=3,
        socket_id="socket-1",
        capabilities=capabilities,
    )


async def _queued_response(coordinator, voice):
    generation = await coordinator.begin_turn("user-1", "client-1")
    return await coordinator.queue(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
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


async def test_generations_are_client_wide_and_strictly_monotonic(coordinator):
    observed = [await coordinator.begin_turn("user-1", "client-1") for _ in range(20)]

    assert observed == list(range(1, 21))


async def test_new_turn_terminally_cancels_the_current_response(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)

    next_generation = await coordinator.begin_turn("user-1", "client-1")

    old = await coordinator.get(response.response_id)
    assert next_generation == response.generation + 1
    assert old.state == "cancelled"
    assert old.terminal_reason == "new_turn"


async def test_synthesis_result_is_dropped_when_generation_changes_during_await(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def synthesize():
        entered.set()
        await release.wait()
        return b"RIFFstale"

    task = asyncio.create_task(coordinator.synthesize(response.response_id, synthesize))
    await entered.wait()
    await coordinator.begin_turn("user-1", "client-1")
    release.set()

    with pytest.raises(StaleResponse):
        await task
    assert (await coordinator.get(response.response_id)).state == "cancelled"


async def test_offer_publishes_binary_wav_reference_only_for_ready_bound_session(
    coordinator, voice_coordinator, redis_client
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id,
        byte_length=12,
        duration_ms=250,
        sample_rate=24000,
    )
    channel = str(device_downlink_channel(ClientId.from_value("client-1")))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    await pubsub.get_message(timeout=1)

    offered = await coordinator.offer(response.response_id, b"RIFF12345678")
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

    assert offered.state == "offered"
    assert message is not None
    assert b'"type":"response.audio"' in message["data"]
    assert b"RIFF12345678" not in message["data"]
    assert await coordinator.read_media(response.response_id) == b"RIFF12345678"


async def test_stale_socket_cannot_offer_or_ack_playback(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id,
        byte_length=12,
        duration_ms=250,
        sample_rate=24000,
    )
    await voice_coordinator.disconnect(
        voice_session_id=voice.voice_session_id, socket_id="socket-1"
    )

    with pytest.raises(StaleResponse):
        await coordinator.offer(response.response_id, b"RIFF12345678")
    with pytest.raises(StaleResponse):
        await coordinator.playback(
            response_id=response.response_id,
            generation=response.generation,
            state="started",
            user_id="user-1",
            client_id="client-1",
            audio_session_id="audio-1",
            voice_session_id=voice.voice_session_id,
            capture_epoch=3,
            socket_id="socket-1",
            monotonic_timestamp_ms=10,
        )


async def test_only_one_response_can_reach_playing(coordinator, voice_coordinator):
    voice = await _ready_voice(voice_coordinator)
    first = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        first.response_id, byte_length=12, duration_ms=250, sample_rate=24000
    )
    await coordinator.offer(first.response_id, b"RIFF12345678")
    playing = await coordinator.playback(
        response_id=first.response_id,
        generation=first.generation,
        state="started",
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        monotonic_timestamp_ms=10,
    )
    assert playing.state == "playing"

    await coordinator.begin_turn("user-1", "client-1")
    second = await coordinator.queue(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        turn_id="turn-2",
        turn_revision=0,
        generation=first.generation + 1,
        kind="speech",
        barge_in_allowed=True,
        trace_id="trace-2",
        causation_id="turn-2",
    )

    with pytest.raises(StaleResponse):
        await coordinator.playback(
            response_id=first.response_id,
            generation=first.generation,
            state="done",
            user_id="user-1",
            client_id="client-1",
            audio_session_id="audio-1",
            voice_session_id=voice.voice_session_id,
            capture_epoch=3,
            socket_id="socket-1",
            monotonic_timestamp_ms=20,
        )
    assert second.state == "queued"


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x00\x00" * 1_600)
    return buffer.getvalue()


async def test_wearable_adapter_requires_full_capture_binding_and_settles_once(
    coordinator, redis_client
):
    await SessionStore(redis_client).init_session(
        "audio-wearable",
        user_id="user-1",
        client_id="wearable-1",
        connection_id="socket-wearable",
        stream_name="audio:stream:audio-wearable",
        capture_epoch=0,
        processing_profile="ambient",
        effects={
            "aec": {"reporting": "unreported"},
            "noise_suppression": {"reporting": "unreported"},
        },
        voice_session_id=None,
    )
    generation = await coordinator.begin_turn("user-1", "wearable-1")
    response = await coordinator.queue_adapter(
        user_id="user-1",
        client_id="wearable-1",
        audio_session_id="audio-wearable",
        turn_id="turn-1",
        turn_revision=0,
        generation=generation,
        kind="speech",
        trace_id="trace-1",
        causation_id="turn-1",
    )
    wav = _wav()
    await coordinator.mark_ready(
        response.response_id,
        byte_length=len(wav),
        duration_ms=100,
        sample_rate=16_000,
    )
    channel = str(device_downlink_channel(ClientId.from_value("wearable-1")))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    await pubsub.get_message(timeout=1)

    done = await coordinator.offer_adapter(response.response_id, wav)
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

    assert done.state == "done"
    payload = json.loads(message["data"])
    assert payload["type"] == "play-audio"
    assert payload["data"]["audio_session_id"] == "audio-wearable"
    assert payload["data"]["generation"] == generation

    with pytest.raises(StaleResponse):
        await coordinator.queue_adapter(
            user_id="user-1",
            client_id="different-client",
            audio_session_id="audio-wearable",
            turn_id="turn-2",
            turn_revision=0,
            generation=generation,
            kind="tone",
            trace_id="trace-2",
            causation_id="turn-2",
        )


async def test_text_delivery_uses_response_audio_for_protocol_v1_phone(
    coordinator, voice_coordinator, redis_client, monkeypatch
):
    voice = await _ready_voice(voice_coordinator)
    await SessionStore(redis_client).init_session(
        "audio-1",
        user_id="user-1",
        client_id="client-1",
        connection_id="socket-1",
        stream_name="audio:stream:audio-1",
        capture_epoch=3,
        processing_profile="duplex_aec",
        effects={
            "aec": {"requested": True, "available": True, "enabled": True},
            "noise_suppression": {
                "requested": True,
                "available": True,
                "enabled": True,
            },
        },
        voice_session_id=voice.voice_session_id,
    )
    monkeypatch.setattr(
        response_delivery, "synthesize_speech", AsyncMock(return_value=_wav())
    )
    generation = await coordinator.begin_turn("user-1", "client-1")
    channel = str(device_downlink_channel(ClientId.from_value("client-1")))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    await pubsub.get_message(timeout=1)

    offered = await deliver_text_response(
        redis_client,
        ClientId.from_value("client-1"),
        SessionId.from_value("audio-1"),
        "hello",
        generation=generation,
        turn_id="turn-1",
    )
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

    assert offered.state == "offered"
    assert offered.transport == "voice_v1"
    assert b'"type":"response.audio"' in message["data"]
    assert b'"audio_b64"' not in message["data"]


async def test_missing_native_playback_ack_fails_current_response_and_health_reports_it(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id,
        byte_length=12,
        duration_ms=250,
        sample_rate=24_000,
    )
    offered = await coordinator.offer(response.response_id, b"RIFF12345678")

    failed = await coordinator.expire_stalled(
        response.response_id,
        now=offered.updated_at + 6,
    )
    health = await coordinator.health("user-1", "client-1")

    assert failed.state == "failed"
    assert failed.terminal_reason == "playback_start_ack_timeout"
    assert health == {
        "generation": response.generation,
        "current_response_id": None,
        "current_state": None,
        "current_updated_at": None,
    }
