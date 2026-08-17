import asyncio
import io
import json
import wave

import pytest
from chronicle_client.voice_session import (
    ServerUpgradeRequired,
    VoiceTargetCapabilities,
    WearableVoiceProtocolError,
    WearableVoiceSession,
)
from chronicle_wearable.playback import ElatoPlaybackTarget


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))


class FakePlaybackTarget:
    native_sample_rate = 24_000
    voice_capabilities = VoiceTargetCapabilities.half_duplex(
        native_sample_rate=native_sample_rate,
        output_route="remote",
        fallback_reason="platform_unavailable",
    )

    def __init__(self) -> None:
        self.played: list[tuple[str, int, bytes]] = []
        self.cancelled: list[tuple[str, int]] = []
        self.release = asyncio.Event()

    async def play(self, *, response_id, generation, wav, report) -> None:
        self.played.append((response_id, generation, wav))
        await report("started", None)
        await self.release.wait()
        await report("done", None)

    async def cancel(self, *, response_id, cancellation_generation) -> None:
        self.cancelled.append((response_id, cancellation_generation))


class FakeElatoSpeaker:
    def __init__(self) -> None:
        self.callback = None
        self.response_id = ""
        self.generation = -1
        self.packets: list[bytes] = []

    def supports_speaker_protocol_v1(self) -> bool:
        return True

    async def subscribe_speaker_status(self, callback) -> None:
        self.callback = callback

    async def speaker_start(self, response_id: str, generation: int) -> None:
        self.response_id = response_id
        self.generation = generation
        self.callback(response_id, generation, "started")

    async def write_speaker_audio(self, packet: bytes) -> None:
        self.packets.append(packet)

    async def speaker_end(self, response_id: str, generation: int) -> None:
        self.callback(response_id, generation, "done")

    async def speaker_stop(
        self, response_id: str, cancellation_generation: int
    ) -> None:
        self.callback(response_id, self.generation, "cancelled")


def _bound(event_type: str, **extra) -> dict:
    return {
        "type": event_type,
        "protocol": 1,
        "event_id": "00000000-0000-4000-8000-000000000001",
        "client_id": "user-elato",
        "audio_session_id": "audio-1",
        "voice_session_id": "voice-1",
        "capture_epoch": 4,
        "sent_at": "2026-08-17T12:00:00Z",
        **extra,
    }


def test_audio_start_advertises_half_duplex_only_with_a_playback_target():
    websocket = FakeWebSocket()
    interactive = WearableVoiceSession(
        websocket, capture_epoch=4, playback_target=FakePlaybackTarget()
    )
    capture_only = WearableVoiceSession(
        websocket, capture_epoch=4, playback_target=None
    )

    assert interactive.audio_start_data() == {
        "rate": 16000,
        "width": 2,
        "channels": 1,
        "mode": "streaming",
        "voice_duplex_protocol": 1,
        "capture_epoch": 4,
        "processing_profile": "half_duplex",
        "effects": {
            "aec": {"requested": False, "available": False, "enabled": False},
            "noise_suppression": {
                "requested": False,
                "available": False,
                "enabled": False,
            },
        },
        "voice_session_id": None,
    }
    assert "voice_duplex_protocol" not in capture_only.audio_start_data()


def test_audio_start_and_ready_use_verified_isolated_target_capabilities():
    target = FakePlaybackTarget()
    target.voice_capabilities = VoiceTargetCapabilities.isolated(
        native_sample_rate=48_000,
        output_route="headphones",
    )
    session = WearableVoiceSession(
        FakeWebSocket(), capture_epoch=4, playback_target=target
    )

    assert session.audio_start_data()["processing_profile"] == "duplex_isolated"


@pytest.mark.asyncio
async def test_interactive_client_fails_closed_when_backend_never_starts_v1():
    session = WearableVoiceSession(
        FakeWebSocket(), capture_epoch=4, playback_target=FakePlaybackTarget()
    )

    with pytest.raises(ServerUpgradeRequired):
        await session.wait_until_ready(timeout=0.001)


@pytest.mark.asyncio
async def test_voice_start_must_match_audio_session_announced_voice_binding():
    session = WearableVoiceSession(
        FakeWebSocket(), capture_epoch=4, playback_target=FakePlaybackTarget()
    )
    await session.handle_event(
        {
            "type": "audio-session.started",
            "protocol": 1,
            "event_id": "00000000-0000-4000-8000-000000000002",
            "client_id": "user-elato",
            "audio_session_id": "audio-1",
            "capture_epoch": 4,
            "processing_profile": "half_duplex",
            "voice_session_id": "voice-1",
            "sent_at": "2026-08-17T12:00:00Z",
        }
    )

    with pytest.raises(WearableVoiceProtocolError):
        await session.handle_event(
            _bound(
                "voice-session.start",
                voice_session_id="voice-2",
                resume_token="r" * 32,
                response_generation=7,
                readiness_deadline_ms=2000,
            )
        )


@pytest.mark.asyncio
async def test_bound_response_is_played_and_acknowledged_through_protocol_v1():
    websocket = FakeWebSocket()
    target = FakePlaybackTarget()
    session = WearableVoiceSession(websocket, capture_epoch=4, playback_target=target)

    await session.handle_event(
        {
            "type": "audio-session.started",
            "protocol": 1,
            "event_id": "00000000-0000-4000-8000-000000000002",
            "client_id": "user-elato",
            "audio_session_id": "audio-1",
            "capture_epoch": 4,
            "processing_profile": "half_duplex",
            "voice_session_id": "voice-1",
            "sent_at": "2026-08-17T12:00:00Z",
        }
    )
    await session.handle_event(
        _bound(
            "voice-session.start",
            resume_token="r" * 32,
            response_generation=7,
            readiness_deadline_ms=2000,
        )
    )
    await session.handle_event(
        _bound(
            "response.audio",
            turn_id="turn-1",
            turn_revision=0,
            response_id="response-1",
            generation=8,
            sequence=0,
            kind="speech",
            barge_in_allowed=True,
            media_type="audio/wav",
            sample_rate=16000,
            byte_length=8,
            duration_ms=250,
            payload_length=8,
            trace_id="trace-1",
            causation_id="turn-1",
        )
    )
    await session.handle_binary(b"RIFFwav!")
    await asyncio.sleep(0)

    assert websocket.sent[0]["type"] == "voice-session.ready"
    assert websocket.sent[0]["capabilities"]["mode"] == "duplex_half"
    assert target.played == [("response-1", 8, b"RIFFwav!")]
    assert websocket.sent[1]["type"] == "response.playback"
    assert websocket.sent[1]["state"] == "started"
    assert websocket.sent[1]["audio_session_id"] == "audio-1"

    target.release.set()
    await session.wait_for_playback()
    assert websocket.sent[2]["state"] == "done"


@pytest.mark.asyncio
async def test_newer_cancellation_stops_the_bound_older_response():
    websocket = FakeWebSocket()
    target = FakePlaybackTarget()
    session = WearableVoiceSession(websocket, capture_epoch=4, playback_target=target)
    await session.handle_event(
        {
            "type": "audio-session.started",
            "protocol": 1,
            "event_id": "00000000-0000-4000-8000-000000000002",
            "client_id": "user-elato",
            "audio_session_id": "audio-1",
            "capture_epoch": 4,
            "processing_profile": "half_duplex",
            "voice_session_id": "voice-1",
            "sent_at": "2026-08-17T12:00:00Z",
        }
    )
    await session.handle_event(
        _bound(
            "voice-session.start",
            resume_token="r" * 32,
            response_generation=7,
            readiness_deadline_ms=2000,
        )
    )
    await session.handle_event(
        _bound(
            "response.audio",
            turn_id="turn-1",
            turn_revision=0,
            response_id="response-1",
            generation=8,
            sequence=0,
            kind="speech",
            barge_in_allowed=True,
            media_type="audio/wav",
            sample_rate=16000,
            byte_length=8,
            duration_ms=250,
            payload_length=8,
            trace_id="trace-1",
            causation_id="turn-1",
        )
    )
    await session.handle_binary(b"RIFFwav!")
    await asyncio.sleep(0)
    await session.handle_event(
        _bound(
            "response.cancel",
            response_id="response-1",
            generation=9,
            reason="barge_in",
        )
    )

    assert target.cancelled == [("response-1", 9)]
    assert websocket.sent[-1]["type"] == "response.playback"
    assert websocket.sent[-1]["state"] == "cancelled"
    assert websocket.sent[-1]["generation"] == 8


@pytest.mark.asyncio
async def test_elato_target_reports_only_firmware_confirmed_playback_states():
    speaker = FakeElatoSpeaker()
    target = ElatoPlaybackTarget(speaker, packet_pacing_seconds=0)
    await target.prepare()
    reports = []
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x00\x00" * 1_600)

    async def report(state, error_code):
        reports.append((state, error_code))

    await target.play(
        response_id="00000000-0000-4000-8000-000000000123",
        generation=8,
        wav=buffer.getvalue(),
        report=report,
    )

    assert reports == [("started", None), ("done", None)]
    assert speaker.packets
