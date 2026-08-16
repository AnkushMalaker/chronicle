from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.controllers import websocket_controller
from advanced_omi_backend.services.voice_frames import (
    VOICE_FRAME_MAXLEN,
    VoiceFramePublisher,
)

pytestmark = pytest.mark.unit


def _metadata(sequence: int = 7) -> dict:
    return {
        "rate": 16000,
        "channels": 1,
        "width": 2,
        "captured_at_ms": 1_770_000_000_125,
        "time_basis": "captured",
        "frame_sequence": sequence,
        "monotonic_offset_ms": sequence * 40,
    }


async def test_interactive_frame_is_fanned_out_before_durable_wal_buffering(
    monkeypatch,
):
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    state = SimpleNamespace(
        stream_session_id="audio-1",
        voice_session_id="voice-1",
        capture_epoch=3,
        processing_profile="duplex_aec",
    )
    producer = SimpleNamespace(redis_client=redis_client)
    order = []
    publish = VoiceFramePublisher.publish
    wal = AsyncMock(return_value=None)

    async def observed_publish(self, **kwargs):
        order.append("turn_stream")
        return await publish(self, **kwargs)

    async def observed_wal(*args, **kwargs):
        order.append("wal")
        return await wal(*args, **kwargs)

    monkeypatch.setattr(VoiceFramePublisher, "publish", observed_publish)
    monkeypatch.setattr(websocket_controller, "_handle_audio_chunk", observed_wal)

    await websocket_controller._handle_pcm_wyoming_audio_packet(
        SimpleNamespace(send_json=AsyncMock()),
        state,
        producer,
        {"data": _metadata()},
        b"\x00\x00" * 640,
        "user-1",
        "user@example.com",
        "client-1",
    )

    entries = await redis_client.xrange("voice:frames:voice-1")
    assert order == ["turn_stream", "wal"]
    assert len(entries) == 1
    assert entries[0][1][b"audio_session_id"] == b"audio-1"
    assert entries[0][1][b"capture_epoch"] == b"3"
    assert entries[0][1][b"frame_sequence"] == b"7"
    assert len(entries[0][1][b"pcm"]) == 1280


async def test_frame_stream_is_bounded_and_non_authoritative():
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    publisher = VoiceFramePublisher(redis_client)
    for sequence in range(VOICE_FRAME_MAXLEN + 25):
        await publisher.publish(
            voice_session_id="voice-1",
            audio_session_id="audio-1",
            capture_epoch=2,
            pcm=b"\x00\x00" * 320,
            metadata=_metadata(sequence),
        )

    assert await redis_client.xlen("voice:frames:voice-1") <= VOICE_FRAME_MAXLEN


@pytest.mark.parametrize(
    "change, message",
    [
        ({"time_basis": "received"}, "time_basis"),
        ({"frame_sequence": -1}, "frame_sequence"),
        ({"monotonic_offset_ms": -1}, "monotonic_offset_ms"),
        ({"captured_at_ms": None}, "captured_at_ms"),
    ],
)
async def test_interactive_frame_requires_native_clock_provenance(change, message):
    metadata = _metadata()
    metadata.update(change)
    publisher = VoiceFramePublisher(fake_aioredis.FakeRedis(decode_responses=False))

    with pytest.raises(ValueError, match=message):
        await publisher.publish(
            voice_session_id="voice-1",
            audio_session_id="audio-1",
            capture_epoch=2,
            pcm=b"\x00\x00" * 320,
            metadata=metadata,
        )
