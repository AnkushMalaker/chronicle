"""Generative checks for the invariants that make duplex replacement safe."""

import asyncio
import json

import pytest
from fakeredis import aioredis as fake_aioredis
from hypothesis import given, settings
from hypothesis import strategies as st

from advanced_omi_backend.models.audio_capabilities import VoiceCapabilities
from advanced_omi_backend.plugins import (
    BasePlugin,
    InteractionModeDefinition,
    InteractionResult,
)
from advanced_omi_backend.plugins.router import PluginRouter
from advanced_omi_backend.services.interaction_modes.contracts import (
    AudioInterval,
    InteractionInput,
)
from advanced_omi_backend.services.interaction_modes.episode_claims import (
    AudioEpisodeArbiter,
)
from advanced_omi_backend.services.interaction_modes.ingress import (
    INPUT_STREAM,
    InteractionIngress,
)
from advanced_omi_backend.services.interaction_modes.processor import (
    InteractionProcessor,
)
from advanced_omi_backend.services.response_coordinator import (
    ResponseCoordinator,
    StaleResponse,
)
from advanced_omi_backend.services.voice_sessions import VoiceSessionCoordinator

pytestmark = pytest.mark.unit
property_settings = settings(max_examples=25, deadline=None)


def _interval(start_ms: float, end_ms: float, *, turn_id: str = "turn-1"):
    return AudioInterval(
        audio_session_id="audio-1",
        capture_epoch=4,
        start_ms=start_ms,
        end_ms=end_ms,
        voice_session_id="voice-1",
        turn_id=turn_id,
    )


@property_settings
@given(
    start_ms=st.integers(min_value=0, max_value=20_000),
    duration_ms=st.integers(min_value=100, max_value=5_000),
    inset_ms=st.integers(min_value=0, max_value=50),
)
def test_property_one_committed_route_per_overlapping_audio_episode(
    start_ms, duration_ms, inset_ms
):
    async def scenario():
        redis_client = fake_aioredis.FakeRedis(decode_responses=True)
        arbiter = AudioEpisodeArbiter(redis_client)
        inset = min(inset_ms, (duration_ms - 1) // 2)
        first, second = await asyncio.gather(
            arbiter.claim(
                user_id="user-1",
                client_id="client-1",
                interval=_interval(start_ms, start_ms + duration_ms),
                source="wake",
            ),
            arbiter.claim(
                user_id="user-1",
                client_id="client-1",
                interval=_interval(
                    start_ms + inset,
                    start_ms + duration_ms - inset,
                ),
                source="committed",
            ),
        )
        assert sum(result.accepted for result in (first, second)) == 1
        assert first.claim.claim_id == second.claim.claim_id

    asyncio.run(scenario())


async def _ready_runtime():
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    voices = VoiceSessionCoordinator(redis_client)
    started = await voices.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        socket_id="socket-1",
        advertised_protocol=2,
    )
    voice = await voices.ready(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        socket_id="socket-1",
        capabilities=VoiceCapabilities(
            mode="duplex_full",
            input_route="built_in_mic",
            output_route="speakerphone",
            native_sample_rate=48_000,
            aec={"requested": True, "available": True, "enabled": True},
            noise_suppression={
                "requested": True,
                "available": True,
                "enabled": True,
            },
            fallback_reason=None,
        ),
    )
    return redis_client, voices, voice


@property_settings
@given(turn_count=st.integers(min_value=2, max_value=12))
def test_property_generations_one_player_and_no_stale_playback(turn_count):
    async def scenario():
        _redis, voices, voice = await _ready_runtime()
        responses = ResponseCoordinator(_redis, voices)
        records = []
        generations = []
        for index in range(turn_count):
            generation = await responses.begin_turn("user-1", "client-1")
            generations.append(generation)
            response = await responses.queue(
                user_id="user-1",
                client_id="client-1",
                audio_session_id="audio-1",
                voice_session_id=voice.voice_session_id,
                capture_epoch=4,
                socket_id="socket-1",
                turn_id=f"turn-{index}",
                turn_revision=0,
                generation=generation,
                kind="speech",
                barge_in_allowed=True,
                trace_id=f"trace-{index}",
                causation_id=f"turn-{index}",
            )
            await responses.mark_ready(
                response.response_id,
                byte_length=len(b"opus-packet"),
                duration_ms=20,
                sample_rate=24_000,
            )
            await responses.offer(response.response_id, (b"opus-packet",))
            records.append(
                await responses.playback(
                    response_id=response.response_id,
                    generation=generation,
                    state="started",
                    user_id="user-1",
                    client_id="client-1",
                    audio_session_id="audio-1",
                    voice_session_id=voice.voice_session_id,
                    capture_epoch=4,
                    socket_id="socket-1",
                    monotonic_timestamp_ms=index,
                )
            )

        assert generations == list(range(1, turn_count + 1))
        stored = [await responses.get(record.response_id) for record in records]
        assert sum(record.state == "playing" for record in stored) == 1
        for stale in records[:-1]:
            with pytest.raises(StaleResponse):
                await responses.playback(
                    response_id=stale.response_id,
                    generation=stale.generation,
                    state="done",
                    user_id="user-1",
                    client_id="client-1",
                    audio_session_id="audio-1",
                    voice_session_id=voice.voice_session_id,
                    capture_epoch=4,
                    socket_id="socket-1",
                    monotonic_timestamp_ms=turn_count,
                )

    asyncio.run(scenario())


@property_settings
@given(redelivery_count=st.integers(min_value=1, max_value=10))
def test_property_effect_fence_never_replays_non_idempotent_mutation(redelivery_count):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class FencedPlugin(BasePlugin):
            INTERACTION_MODES = (
                InteractionModeDefinition(
                    mode_id="checkout",
                    activation_phrases=("place order",),
                ),
            )

            def __init__(self):
                super().__init__({"enabled": True, "modes": ["checkout"]})
                self.mutations = 0

            async def initialize(self):
                return None

            async def on_interaction_start(self, context):
                context.session.plugin_state = {"intent": "checkout"}
                await context.checkpoint()
                self.mutations += 1
                entered.set()
                await release.wait()
                return InteractionResult(
                    reply="Order placed",
                    phase="finished",
                    plugin_state={"order": "committed"},
                    end=True,
                )

        redis_client = fake_aioredis.FakeRedis(decode_responses=True)
        plugin = FencedPlugin()
        router = PluginRouter()
        router.register_plugin("checkout", plugin)
        await InteractionIngress(redis_client, router.interaction_registry).submit(
            user_id="user-1",
            client_id="client-1",
            audio_interval=_interval(100, 900),
            text="place order",
            source="committed",
        )
        entries = await redis_client.xrange(INPUT_STREAM)
        item = InteractionInput.from_dict(json.loads(entries[0][1]["input"]))
        processor = InteractionProcessor(redis_client, router)
        task = asyncio.create_task(processor.process(item))
        await entered.wait()
        await ResponseCoordinator(
            redis_client, VoiceSessionCoordinator(redis_client)
        ).begin_turn("user-1", "client-1")
        release.set()
        dispatch = await task
        assert dispatch.reply is None
        for _ in range(redelivery_count):
            assert await processor.process(item) is None
        assert plugin.mutations == 1

    asyncio.run(scenario())
