import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.models.audio_capabilities import VoiceCapabilities
from advanced_omi_backend.services.audio_stream.session_store import SessionStore
from advanced_omi_backend.services.interaction_modes.committed_turns import (
    CommittedAudioTurn,
    CommittedTranscriptAssembler,
    CommittedTurnRouter,
)
from advanced_omi_backend.services.interaction_modes.contracts import (
    AudioInterval,
    InteractionInput,
    InteractionModeDefinition,
)
from advanced_omi_backend.services.interaction_modes.episode_claims import (
    AudioEpisodeArbiter,
)
from advanced_omi_backend.services.interaction_modes.ingress import INPUT_STREAM
from advanced_omi_backend.services.interaction_modes.registry import InteractionRegistry
from advanced_omi_backend.services.interaction_modes.store import InteractionStore
from advanced_omi_backend.services.response_coordinator import ResponseCoordinator
from advanced_omi_backend.services.voice_sessions import VoiceSessionCoordinator
from advanced_omi_backend.services.wakeword.activations import (
    WakeActivation,
    WakeActivationStore,
)

pytestmark = pytest.mark.unit


def _interval(start_ms: float, end_ms: float) -> AudioInterval:
    return AudioInterval(
        audio_session_id="audio-1",
        capture_epoch=4,
        start_ms=start_ms,
        end_ms=end_ms,
        voice_session_id="voice-1",
        turn_id="turn-1",
    )


def _turn_fields(
    voice_session_id: str = "voice-1",
    *,
    turn_id: str = "turn-1",
    start_ms: int = 200,
    end_ms: int = 1000,
) -> dict:
    return {
        "turn_id": turn_id,
        "turn_revision": "0",
        "voice_session_id": voice_session_id,
        "audio_session_id": "audio-1",
        "capture_epoch": "4",
        "start_sequence": "5",
        "end_sequence": "25",
        "started_at_ms": str(start_ms),
        "ended_at_ms": str(end_ms),
        "sample_rate": "16000",
        "channels": "1",
        "sample_width": "2",
        "pcm": b"\x01\x00" * 12_800,
    }


async def test_audio_episode_interval_has_exactly_one_atomic_route():
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    arbiter = AudioEpisodeArbiter(redis_client)
    first, second = await asyncio.gather(
        arbiter.claim(
            user_id="user-1",
            client_id="client-1",
            interval=_interval(200, 1000),
            source="streaming",
        ),
        arbiter.claim(
            user_id="user-1",
            client_id="client-1",
            interval=_interval(240, 960),
            source="wake",
        ),
    )

    assert sum(result.accepted for result in (first, second)) == 1
    assert first.claim.claim_id == second.claim.claim_id


async def test_final_fragments_dispatch_only_after_watermark_passes_interval():
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    await redis_client.xadd(
        "transcription:results:audio-1",
        {
            "words": json.dumps(
                [
                    {"word": "order", "start": 0.3, "end": 0.5},
                    {"word": "swiggy", "start": 0.55, "end": 1.1},
                ]
            )
        },
    )
    batch = AsyncMock(return_value="must not run")
    assembler = CommittedTranscriptAssembler(
        redis_client,
        exact_transcriber=batch,
        watermark_wait_seconds=0,
    )

    result = await assembler.resolve(CommittedAudioTurn.from_fields(_turn_fields()))

    assert result.text == "order swiggy"
    assert result.source == "streaming_final"
    batch.assert_not_awaited()


async def test_known_partial_text_is_never_dispatched_and_exact_range_fills_gap():
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    await redis_client.xadd(
        "transcription:results:audio-1",
        {"words": json.dumps([{"word": "partial", "start": 0.2, "end": 0.5}])},
    )
    batch = AsyncMock(return_value="complete order")
    assembler = CommittedTranscriptAssembler(
        redis_client,
        exact_transcriber=batch,
        watermark_wait_seconds=0,
    )

    result = await assembler.resolve(CommittedAudioTurn.from_fields(_turn_fields()))

    assert result.text == "complete order"
    assert result.source == "exact_range_batch"
    batch.assert_awaited_once()


def _capabilities() -> VoiceCapabilities:
    return VoiceCapabilities(
        mode="duplex_full",
        input_route="built_in_mic",
        output_route="speakerphone",
        native_sample_rate=48_000,
        aec={"requested": True, "available": True, "enabled": True},
        noise_suppression={"requested": True, "available": True, "enabled": True},
        fallback_reason=None,
    )


async def test_real_committed_router_validates_binding_and_enqueues_complete_turn():
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    voice_coordinator = VoiceSessionCoordinator(redis_client)
    started = await voice_coordinator.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        socket_id="socket-1",
        advertised_protocol=2,
    )
    voice_id = started.session.voice_session_id
    await voice_coordinator.ready(
        voice_session_id=voice_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        socket_id="socket-1",
        capabilities=_capabilities(),
    )
    await SessionStore(redis_client).init_session(
        "audio-1",
        user_id="user-1",
        client_id="client-1",
        connection_id="socket-1",
        stream_name="audio:stream:audio-1",
        capture_epoch=4,
        processing_profile="duplex_aec",
        effects={
            "aec": {"requested": True, "available": True, "enabled": True},
            "noise_suppression": {
                "requested": True,
                "available": True,
                "enabled": True,
            },
        },
        voice_session_id=voice_id,
    )
    registry = InteractionRegistry()
    registry.register(
        "swiggy",
        InteractionModeDefinition(
            mode_id="swiggy_order",
            activation_phrases=("order swiggy",),
        ),
    )
    assembler = CommittedTranscriptAssembler(
        redis_client,
        exact_transcriber=AsyncMock(return_value="order swiggy add milk"),
        watermark_wait_seconds=0,
    )
    router = CommittedTurnRouter(
        redis_client,
        registry,
        transcript_assembler=assembler,
    )

    result = await router.route(_turn_fields(voice_id))

    assert result.accepted and result.reason == "start"
    entries = await redis_client.xrange(INPUT_STREAM)
    raw = entries[0][1][b"input"]
    item = InteractionInput.from_dict(json.loads(raw))
    assert item.text == "add milk"
    assert item.response_generation == 1
    assert item.audio_interval.voice_session_id == voice_id
    assert item.audio_interval.capture_epoch == 4

    duplicate = await router.route(_turn_fields(voice_id))
    assert not duplicate.accepted
    assert duplicate.reason == "episode_already_claimed"
    assert (
        await ResponseCoordinator(redis_client, voice_coordinator).current_generation(
            "user-1", "client-1"
        )
        == 1
    )

    active = await InteractionStore(redis_client).get_active("user-1", "client-1")
    await InteractionStore(redis_client).end(active, reason="test_complete")

    command_dispatcher = AsyncMock()
    ordinary_router = CommittedTurnRouter(
        redis_client,
        InteractionRegistry(),
        transcript_assembler=CommittedTranscriptAssembler(
            redis_client,
            exact_transcriber=AsyncMock(return_value="turn on the lights"),
            watermark_wait_seconds=0,
        ),
        command_dispatcher=command_dispatcher,
    )
    ordinary_fields = _turn_fields(
        voice_id,
        turn_id="turn-2",
        start_ms=2_000,
        end_ms=2_800,
    )

    ordinary = await ordinary_router.route(ordinary_fields)

    assert ordinary.consumed and not ordinary.accepted
    assert ordinary.reason == "not_addressed"
    command_dispatcher.assert_not_awaited()
    assert (
        await ResponseCoordinator(redis_client, voice_coordinator).current_generation(
            "user-1", "client-1"
        )
        == 1
    )

    activation = WakeActivation(
        wake_trace_id="7ce4d46b-232f-47f9-8148-d595ed344cf2",
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        wakeword="hey_hermes",
        armed_at=1_770_000_001.0,
        end_of_turn_at=1_770_000_002.0,
        command_start_ms=3_200,
        command_end_ms=4_000,
    )
    await WakeActivationStore(redis_client).register(activation)
    addressed_fields = _turn_fields(
        voice_id,
        turn_id="turn-3",
        start_ms=3_000,
        end_ms=4_400,
    )

    addressed = await ordinary_router.route(addressed_fields)

    assert addressed.accepted and addressed.reason == "wake_command"
    dispatched_turn = CommittedAudioTurn.from_fields(addressed_fields)
    command_dispatcher.assert_awaited_once_with(
        dispatched_turn,
        "turn on the lights",
        "user-1",
        "client-1",
        2,
        activation,
    )


async def test_committed_router_rejects_stale_voice_binding_before_transcription():
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    assembler = CommittedTranscriptAssembler(
        redis_client,
        exact_transcriber=AsyncMock(return_value="order swiggy"),
        watermark_wait_seconds=0,
    )
    router = CommittedTurnRouter(
        redis_client,
        InteractionRegistry(),
        transcript_assembler=assembler,
    )

    with pytest.raises(ValueError, match="no audio session"):
        await router.route(_turn_fields("stale-voice"))

    assembler.exact_transcriber.assert_not_awaited()


async def test_committed_router_redelivers_pending_turn_before_new_work():
    redis_client = AsyncMock()
    redis_client.xreadgroup.side_effect = [
        [(b"voice:turns:committed", [(b"1-0", _turn_fields())])],
        [],
    ]
    redis_client.xautoclaim.return_value = (b"0-0", [], [])
    router = CommittedTurnRouter(redis_client, InteractionRegistry())
    router.route = AsyncMock()

    recovered = await router.recover_pending(claim_min_idle_ms=0)

    assert recovered == 1
    router.route.assert_awaited_once_with(_turn_fields())
    redis_client.xack.assert_awaited_once_with(
        "voice:turns:committed", "committed-turn-router", b"1-0"
    )
    first_read = redis_client.xreadgroup.await_args_list[0]
    assert first_read.args[2] == {"voice:turns:committed": "0"}


async def test_committed_router_acks_invalid_binary_turn_without_killing_worker():
    redis_client = AsyncMock()
    router = CommittedTurnRouter(redis_client, InteractionRegistry())
    router.route = AsyncMock(side_effect=ValueError("stale voice binding"))

    await router._handle_entry(b"1-0", _turn_fields())

    redis_client.xack.assert_awaited_once_with(
        "voice:turns:committed", "committed-turn-router", b"1-0"
    )


async def test_committed_router_leaves_transient_failure_pending_without_exiting():
    redis_client = AsyncMock()
    router = CommittedTurnRouter(redis_client, InteractionRegistry())
    router.route = AsyncMock(side_effect=RuntimeError("provider unavailable"))

    await router._handle_entry(b"1-0", _turn_fields())

    redis_client.xack.assert_not_awaited()
