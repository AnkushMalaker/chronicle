import asyncio

import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.redis_keys import ClientId, device_downlink_channel
from advanced_omi_backend.services.response_coordinator import (
    ResponseCoordinator,
    StaleResponse,
)
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
