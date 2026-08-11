import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from consumer import GROUP_NAME, WakeWordConsumer
from detector import WakeEvent


class FakeRedis:
    def __init__(self):
        self.read_count = 0

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


class FakeDetector:
    def __init__(self):
        self.calls = []

    def new_client_state(self):
        return SimpleNamespace(armed=False, priming=False)

    async def process_frame(self, state, client_id, session_id, pcm):
        self.calls.append((client_id, session_id))
        return WakeEvent(
            client_id=client_id,
            session_id=session_id,
            wakeword="hey_hermes",
            audio=pcm,
            arm_time=1.0,
            eot_time=2.0,
            score=0.99,
            reason="test",
        )

    def flush(self, state, client_id, session_id):
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

    await consumer._process_stream("audio:stream:session-uuid", "session-uuid")

    assert detector.calls == [("a421c9-elato", "session-uuid")]
    assert [(event.client_id, event.session_id) for event in events] == [
        ("a421c9-elato", "session-uuid")
    ]
