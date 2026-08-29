from types import SimpleNamespace
from unittest.mock import AsyncMock

import opuslib
import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.controllers import websocket_controller

pytestmark = pytest.mark.unit


def _opus_packet() -> bytes:
    pcm = b"\x00\x00" * 640
    encoder = opuslib.Encoder(16_000, 1, opuslib.APPLICATION_VOIP)
    return encoder.encode(pcm, 640)


async def test_interactive_opus_packet_decodes_before_fanout_and_durable_wal(
    monkeypatch,
):
    packet = _opus_packet()
    redis_client = fake_aioredis.FakeRedis(decode_responses=False)
    state = SimpleNamespace(
        stream_session_id="audio-1",
        voice_session_id="voice-1",
        capture_epoch=3,
        processing_profile="duplex_aec",
    )
    producer = SimpleNamespace(redis_client=redis_client)
    durable = AsyncMock(return_value=None)
    monkeypatch.setattr(websocket_controller, "_handle_audio_chunk", durable)

    task, accepted = await websocket_controller._handle_opus_wyoming_audio_packet(
        SimpleNamespace(send_json=AsyncMock()),
        state,
        producer,
        {
            "type": "audio-chunk",
            "data": {
                "codec": "opus",
                "rate": 16_000,
                "channels": 1,
                "frame_duration_ms": 40,
                "time_basis": "captured",
                "frame_sequence": 7,
                "monotonic_offset_ms": 280,
                "captured_at_ms": 1_770_000_000_125,
            },
            "payload_length": len(packet),
        },
        packet,
        "user-1",
        "user@example.com",
        "client-1",
    )

    assert accepted is True
    assert task is None
    entries = await redis_client.xrange("voice:frames:voice-1")
    assert len(entries) == 1
    assert entries[0][1][b"frame_sequence"] == b"7"
    assert len(entries[0][1][b"pcm"]) == 1_280
    assert durable.await_count == 1
    assert durable.await_args.args[2] == entries[0][1][b"pcm"]
    assert durable.await_args.args[3]["width"] == 2


async def test_interactive_opus_header_rejects_container_or_mismatched_payload():
    packet = _opus_packet()
    state = SimpleNamespace(
        stream_session_id="audio-1",
        voice_session_id="voice-1",
        capture_epoch=3,
        processing_profile="duplex_aec",
    )
    producer = SimpleNamespace(
        redis_client=fake_aioredis.FakeRedis(decode_responses=False)
    )
    header = {
        "type": "audio-chunk",
        "data": {
            "codec": "opus",
            "rate": 16_000,
            "channels": 1,
            "frame_duration_ms": 40,
            "time_basis": "captured",
            "frame_sequence": 0,
            "monotonic_offset_ms": 0,
            "captured_at_ms": 1_770_000_000_125,
        },
        "payload_length": len(packet) + 1,
    }

    with pytest.raises(ValueError, match="payload_length"):
        await websocket_controller._handle_opus_wyoming_audio_packet(
            SimpleNamespace(send_json=AsyncMock()),
            state,
            producer,
            header,
            packet,
            "user-1",
            "user@example.com",
            "client-1",
        )
