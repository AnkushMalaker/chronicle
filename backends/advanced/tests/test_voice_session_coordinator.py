import hashlib

import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.services.voice_sessions import (
    ClientUpgradeRequired,
    StaleVoiceBinding,
    VoiceSessionCoordinator,
)
from advanced_omi_backend.voice_protocol import VoiceCapabilities

pytestmark = pytest.mark.unit


def _capabilities(mode: str = "duplex_full") -> VoiceCapabilities:
    if mode == "duplex_full":
        return VoiceCapabilities(
            mode=mode,
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
    return VoiceCapabilities(
        mode="duplex_half",
        input_route="built_in_mic",
        output_route="speakerphone",
        native_sample_rate=48000,
        aec={"requested": True, "available": False, "enabled": False},
        noise_suppression={
            "requested": True,
            "available": True,
            "enabled": True,
        },
        fallback_reason="aec_unavailable",
    )


@pytest.fixture
def redis_client():
    return fake_aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def coordinator(redis_client):
    return VoiceSessionCoordinator(redis_client)


async def _start(coordinator: VoiceSessionCoordinator):
    return await coordinator.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        socket_id="socket-1",
        advertised_protocol=1,
    )


async def test_interactive_activation_requires_protocol_v1(coordinator):
    with pytest.raises(ClientUpgradeRequired):
        await coordinator.start(
            user_id="user-1",
            client_id="client-1",
            audio_session_id="audio-1",
            capture_epoch=0,
            socket_id="socket-1",
            advertised_protocol=None,
        )


async def test_start_stores_only_hashed_resume_proof(coordinator, redis_client):
    started = await _start(coordinator)

    stored = await coordinator.get(started.session.voice_session_id)
    raw_hash = await redis_client.hgetall(
        f"voice:session:{started.session.voice_session_id}"
    )

    assert stored == started.session
    assert len(started.resume_token) >= 32
    assert started.resume_token not in raw_hash.values()
    assert (
        raw_hash["resume_token_hash"]
        == hashlib.sha256(started.resume_token.encode()).hexdigest()
    )


async def test_ready_requires_the_complete_authenticated_socket_binding(coordinator):
    started = await _start(coordinator)

    with pytest.raises(StaleVoiceBinding):
        await coordinator.ready(
            voice_session_id=started.session.voice_session_id,
            user_id="user-1",
            client_id="client-1",
            audio_session_id="wrong-audio",
            capture_epoch=4,
            socket_id="socket-1",
            capabilities=_capabilities(),
        )

    ready = await coordinator.ready(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        socket_id="socket-1",
        capabilities=_capabilities(),
    )
    assert ready.state == "ready_full"


async def test_route_change_rebinds_capture_and_increments_generation(coordinator):
    started = await _start(coordinator)
    await coordinator.ready(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        socket_id="socket-1",
        capabilities=_capabilities(),
    )

    changed = await coordinator.capabilities_changed(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-2",
        capture_epoch=5,
        socket_id="socket-1",
        capabilities=_capabilities("duplex_half"),
        reason="route_changed",
    )

    assert changed.state == "reconfiguring"
    assert changed.audio_session_id == "audio-2"
    assert changed.capture_epoch == 5
    assert changed.generation == 1


async def test_disconnect_suppresses_output_and_single_use_resume_rotates_identity(
    coordinator, redis_client
):
    started = await _start(coordinator)
    disconnected = await coordinator.disconnect(
        voice_session_id=started.session.voice_session_id,
        socket_id="socket-1",
    )
    assert disconnected.state == "reconnecting"
    assert disconnected.generation == 1
    raw = await redis_client.hgetall(
        f"voice:session:{started.session.voice_session_id}"
    )
    assert raw["resume_from_generation"] == "0"

    resumed = await coordinator.resume(
        previous_voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        previous_capture_epoch=4,
        resume_token=started.resume_token,
        new_audio_session_id="audio-2",
        new_capture_epoch=5,
        new_socket_id="socket-2",
        last_response_generation=0,
    )

    assert resumed.session.voice_session_id != started.session.voice_session_id
    assert resumed.session.audio_session_id == "audio-2"
    assert resumed.session.capture_epoch == 5
    assert resumed.session.state == "starting"
    assert resumed.resume_token != started.resume_token
    assert (await coordinator.get(started.session.voice_session_id)).state == "ended"

    with pytest.raises(StaleVoiceBinding):
        await coordinator.resume(
            previous_voice_session_id=started.session.voice_session_id,
            user_id="user-1",
            client_id="client-1",
            previous_capture_epoch=4,
            resume_token=started.resume_token,
            new_audio_session_id="audio-3",
            new_capture_epoch=6,
            new_socket_id="socket-3",
            last_response_generation=0,
        )


async def test_fresh_activation_ends_reconnecting_session_instead_of_resuming_it(
    coordinator,
):
    first = await _start(coordinator)
    await coordinator.disconnect(
        voice_session_id=first.session.voice_session_id, socket_id="socket-1"
    )

    fresh = await coordinator.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="unrelated-audio",
        capture_epoch=0,
        socket_id="socket-2",
        advertised_protocol=1,
    )

    assert fresh.session.voice_session_id != first.session.voice_session_id
    old = await coordinator.get(first.session.voice_session_id)
    assert old.state == "ended"
    assert old.end_reason == "audio_disconnect"
