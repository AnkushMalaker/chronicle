import asyncio
import json

import pytest
from audio_contract.v2 import audio_pb2
from chronicle_client.audio_v2 import AudioV2Client
from google.protobuf import json_format, timestamp_pb2

pytestmark = pytest.mark.asyncio


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, value):
        self.sent.append(value)

    async def close(self):
        self.closed = True
        await self.incoming.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value


def _control(**event):
    message = audio_pb2.ServerControl(
        event_id=audio_pb2.EventId(value="event-1"),
        sent_at=timestamp_pb2.Timestamp(seconds=1),
        **event,
    )
    return json_format.MessageToJson(
        message, preserving_proto_field_name=True, indent=None
    )


async def test_client_sends_atomic_bound_opus_packet(monkeypatch):
    socket = FakeSocket()

    async def connect(*_args, **_kwargs):
        return socket

    monkeypatch.setattr("chronicle_client.audio_v2.websockets.connect", connect)
    client = AudioV2Client(
        websocket_url="wss://chronicle/ws/audio",
        bearer_token="token",
        source_id="neo",
        display_name="Neo",
        device_kind=audio_pb2.DEVICE_KIND_NEO,
    )
    connect_task = asyncio.create_task(client.connect())
    await asyncio.sleep(0)
    await socket.incoming.put(
        _control(
            hello=audio_pb2.ServerHello(
                client_id=audio_pb2.ClientId(value="client-1"),
                connection_id=audio_pb2.ConnectionId(value="connection-1"),
            )
        )
    )
    await connect_task
    start_task = asyncio.create_task(client.start_capture(capture_epoch=4))
    await asyncio.sleep(0)
    binding = audio_pb2.CaptureBinding(
        capture_session_id=audio_pb2.CaptureSessionId(value="capture-1"),
        capture_epoch=4,
    )
    await socket.incoming.put(
        _control(
            capture_started=audio_pb2.CaptureStarted(
                binding=binding, audio_spec=client.opus_spec(16_000)
            )
        )
    )
    await start_task

    await client.send_opus(b"raw-opus", captured_at=100.0)
    envelope = audio_pb2.MediaEnvelope.FromString(socket.sent[-1])

    assert envelope.capture.binding == binding
    assert envelope.capture.sequence == 0
    assert envelope.capture.opus_payload == b"raw-opus"
    assert json.loads(socket.sent[0])["hello"]["bearer_token"] == "token"
    await socket.close()
    await asyncio.gather(client._receive_task, return_exceptions=True)
