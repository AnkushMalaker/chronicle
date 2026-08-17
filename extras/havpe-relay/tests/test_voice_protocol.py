import asyncio
import json

from chronicle_client.voice_session import VoiceTargetCapabilities, WearableVoiceSession
from device_controller import DeviceController
from relay_core import forward_tcp_to_ws, handle_backend_messages, send_audio_start
from voice_playback import HavpePlaybackTarget


class FakeWebSocket:
    def __init__(self, incoming=()) -> None:
        self.incoming = list(incoming)
        self.sent = []

    async def send(self, value) -> None:
        self.sent.append(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)


class ImmediateTarget:
    native_sample_rate = 48_000
    voice_capabilities = VoiceTargetCapabilities.half_duplex(
        native_sample_rate=native_sample_rate,
        output_route="remote",
        fallback_reason="platform_unavailable",
    )

    async def play(self, *, response_id, generation, wav, report) -> None:
        await report("started", None)
        await report("done", None)

    async def cancel(self, *, response_id, cancellation_generation) -> None:
        return None


class FakeDevice:
    def __init__(self) -> None:
        self.states = asyncio.Queue()
        self.played = []
        self.stopped = 0

    def clear_playback_states(self) -> None:
        while not self.states.empty():
            self.states.get_nowait()

    async def play_audio(self, url: str, announcement: bool = True) -> None:
        self.played.append((url, announcement))
        self.states.put_nowait("started")
        self.states.put_nowait("stopped")

    async def wait_playback_state(self, expected: str, timeout: float) -> None:
        while True:
            if await asyncio.wait_for(self.states.get(), timeout) == expected:
                return

    async def stop_audio(self) -> None:
        self.stopped += 1
        self.states.put_nowait("stopped")


def _audio_started() -> dict:
    return {
        "type": "audio-session.started",
        "protocol": 1,
        "event_id": "00000000-0000-4000-8000-000000000001",
        "client_id": "user-havpe",
        "audio_session_id": "audio-1",
        "capture_epoch": 1,
        "processing_profile": "half_duplex",
        "voice_session_id": "voice-1",
        "sent_at": "2026-08-17T12:00:00Z",
    }


def _bound(event_type: str, **extra) -> dict:
    return {
        "type": event_type,
        "protocol": 1,
        "event_id": "00000000-0000-4000-8000-000000000002",
        "client_id": "user-havpe",
        "audio_session_id": "audio-1",
        "voice_session_id": "voice-1",
        "capture_epoch": 1,
        "sent_at": "2026-08-17T12:00:00Z",
        **extra,
    }


def test_relay_replaces_firmware_audio_start_with_half_duplex_v1():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"type":"audio-start","data":{"rate":16000,"width":2,'
            b'"channels":1,"mode":"streaming"},"payload_length":0}\n'
        )
        reader.feed_eof()
        websocket = FakeWebSocket()
        session = WearableVoiceSession(
            websocket, capture_epoch=1, playback_target=ImmediateTarget()
        )

        async def complete_server_handshake():
            while not websocket.sent:
                await asyncio.sleep(0)
            await session.handle_event(_audio_started())
            await session.handle_event(
                _bound(
                    "voice-session.start",
                    resume_token="r" * 32,
                    response_generation=0,
                    readiness_deadline_ms=2000,
                )
            )

        handshake = asyncio.create_task(complete_server_handshake())

        await forward_tcp_to_ws(
            reader,
            websocket,
            asyncio.Lock(),
            voice_session=session,
        )
        await handshake

        header = json.loads(websocket.sent[0])
        assert header["data"]["voice_duplex_protocol"] == 1
        assert header["data"]["processing_profile"] == "half_duplex"
        assert header["data"]["capture_epoch"] == 1

    asyncio.run(scenario())


def test_relay_replays_audio_start_when_backend_websocket_reconnects():
    async def scenario():
        websocket = FakeWebSocket()
        session = WearableVoiceSession(
            websocket, capture_epoch=2, playback_target=ImmediateTarget()
        )
        template = {
            "type": "audio-start",
            "data": {"rate": 16000, "width": 2, "channels": 1},
            "payload_length": 0,
        }

        async def complete_server_handshake():
            while not websocket.sent:
                await asyncio.sleep(0)
            started = _audio_started()
            started["capture_epoch"] = 2
            await session.handle_event(started)
            start = _bound(
                "voice-session.start",
                resume_token="r" * 32,
                response_generation=0,
                readiness_deadline_ms=2000,
            )
            start["capture_epoch"] = 2
            await session.handle_event(start)

        handshake = asyncio.create_task(complete_server_handshake())
        await send_audio_start(websocket, asyncio.Lock(), session, template)
        await handshake

        replayed = json.loads(websocket.sent[0])
        assert replayed["data"]["capture_epoch"] == 2
        assert replayed["data"]["voice_duplex_protocol"] == 1

    asyncio.run(scenario())


def test_relay_consumes_bound_binary_response_and_returns_playback_acks():
    async def scenario():
        audio = _bound(
            "response.audio",
            turn_id="turn-1",
            turn_revision=0,
            response_id="response-1",
            generation=1,
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
        websocket = FakeWebSocket(
            [
                json.dumps(_audio_started()),
                json.dumps(
                    _bound(
                        "voice-session.start",
                        resume_token="r" * 32,
                        response_generation=0,
                        readiness_deadline_ms=2000,
                    )
                ),
                json.dumps(audio),
                b"RIFFwav!",
            ]
        )
        session = WearableVoiceSession(
            websocket, capture_epoch=1, playback_target=ImmediateTarget()
        )

        await handle_backend_messages(websocket, session)
        await session.wait_for_playback()

        sent = [json.loads(value) for value in websocket.sent]
        assert [event["type"] for event in sent] == [
            "voice-session.ready",
            "response.playback",
            "response.playback",
        ]
        assert [event.get("state") for event in sent[1:]] == ["started", "done"]

    asyncio.run(scenario())


def test_havpe_target_acks_observed_firmware_states():
    async def scenario():
        device = FakeDevice()
        target = HavpePlaybackTarget(
            device, stage_audio=lambda _wav: "http://relay/response.wav"
        )
        reports = []

        async def report(state, error_code):
            reports.append((state, error_code))

        await target.play(
            response_id="response-1",
            generation=1,
            wav=b"RIFFwav!",
            report=report,
        )

        assert device.played == [("http://relay/response.wav", True)]
        assert reports == [("started", None), ("done", None)]

    asyncio.run(scenario())


def test_firmware_playback_state_is_broadcast_to_play_and_cancel_waiters():
    async def scenario():
        device = DeviceController()
        play_waiter = asyncio.create_task(
            device.wait_playback_state("stopped", timeout=0.1)
        )
        cancel_waiter = asyncio.create_task(
            device.wait_playback_state("stopped", timeout=0.1)
        )
        await asyncio.sleep(0)

        device._publish_playback_state("stopped")

        await asyncio.gather(play_waiter, cancel_waiter)

    asyncio.run(scenario())


def test_cancelled_havpe_playback_never_reports_done():
    class InterruptibleDevice:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()

        def clear_playback_states(self):
            self.started.clear()
            self.stopped.clear()

        async def play_audio(self, _url, announcement=True):
            self.started.set()

        async def stop_audio(self):
            self.stopped.set()

        async def wait_playback_state(self, expected, timeout):
            event = self.started if expected == "started" else self.stopped
            await asyncio.wait_for(event.wait(), timeout)

    async def scenario():
        target = HavpePlaybackTarget(
            InterruptibleDevice(), stage_audio=lambda _wav: "http://relay/a.wav"
        )
        reports = []

        async def report(state, error_code):
            reports.append((state, error_code))

        playback = asyncio.create_task(
            target.play(
                response_id="response-1",
                generation=1,
                wav=b"RIFFwav!",
                report=report,
            )
        )
        while not reports:
            await asyncio.sleep(0)
        await target.cancel(response_id="response-1", cancellation_generation=2)
        await playback

        assert reports == [("started", None)]

    asyncio.run(scenario())
