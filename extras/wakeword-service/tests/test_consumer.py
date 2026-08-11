import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from consumer import GROUP_NAME, WakeWordConsumer
from detector import WakeEvent
from identities import AudioStreamName, ClientId, SessionId, device_downlink_channel


class FakeRedis:
    def __init__(self):
        self.read_count = 0
        self.published = []

    async def hget(self, key, field):
        assert key == "audio:session:session-uuid"
        assert field == "client_id"
        return b"a421c9-elato"

    async def xgroup_create(self, *args, **kwargs):
        return None

    async def xreadgroup(self, group, consumer, streams, count, block):
        assert group == GROUP_NAME
        assert streams == {"audio:stream:session-uuid": ">"}
        self.read_count += 1
        if self.read_count == 1:
            return [
                (
                    b"audio:stream:session-uuid",
                    [(b"1-0", {b"audio_data": b"\x00\x00"})],
                )
            ]
        return [
            (
                b"audio:stream:session-uuid",
                [(b"2-0", {b"end_marker": b"true"})],
            )
        ]

    async def xack(self, *args):
        return 1

    async def publish(self, channel, message):
        self.published.append((channel, message))
        return 1


class FakeDetector:
    def __init__(self):
        self.calls = []

    def new_client_state(self):
        return SimpleNamespace(armed=False, priming=False)

    async def process_frame(self, state, session_ref, pcm):
        self.calls.append((str(session_ref.client_id), str(session_ref.session_id)))
        return WakeEvent(
            client_id=session_ref.client_id,
            session_id=session_ref.session_id,
            wakeword="hey_hermes",
            audio=pcm,
            arm_time=1.0,
            eot_time=2.0,
            score=0.99,
            reason="test",
        )

    def flush(self, state, session_ref):
        return None


@pytest.mark.asyncio
async def test_process_stream_keeps_client_and_session_id_distinct():
    detector = FakeDetector()
    consumer = WakeWordConsumer(detector, "redis://unused", SimpleNamespace())
    consumer.redis_client = FakeRedis()
    consumer.running = True
    events = []

    async def capture_event(event):
        events.append(event)

    consumer._handle_event = capture_event

    await consumer._process_stream(
        AudioStreamName.from_value("audio:stream:session-uuid"),
        SessionId.from_value("session-uuid"),
    )

    assert detector.calls == [("a421c9-elato", "session-uuid")]
    assert [(str(event.client_id), str(event.session_id)) for event in events] == [
        ("a421c9-elato", "session-uuid")
    ]


def test_device_downlink_channel_requires_client_identity():
    assert str(device_downlink_channel(ClientId.from_value("a421c9-elato"))) == (
        "device:downlink:a421c9-elato"
    )

    with pytest.raises(TypeError, match="ClientId"):
        device_downlink_channel(SessionId.from_value("session-uuid"))
