from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.controllers import websocket_controller
from advanced_omi_backend.workers.audio_jobs import _captured_at_from_fields


def test_mobile_capture_time_takes_precedence_over_redis_arrival_time():
    captured = datetime(2026, 8, 9, 12, 34, 56, tzinfo=timezone.utc)
    fields = {b"captured_at": str(captured.timestamp()).encode()}

    assert _captured_at_from_fields(fields, "1999999999000-0") == captured


def test_invalid_mobile_capture_time_falls_back_to_redis_stream_time():
    stream_id = "1786278896000-0"

    assert _captured_at_from_fields(
        {b"captured_at": b"invalid"}, stream_id
    ) == datetime.fromtimestamp(1786278896, tz=timezone.utc)


@pytest.mark.asyncio
async def test_pcm_durable_packet_is_acked_only_after_crossing_the_wal(monkeypatch):
    redis = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
    producer = SimpleNamespace(
        redis_client=redis,
        flush_session_buffer=AsyncMock(return_value="wal-message-1"),
    )
    websocket = SimpleNamespace(send_json=AsyncMock())
    state = SimpleNamespace(
        stream_session_id="capture-1",
        voice_session_id=None,
        processing_profile="ambient",
    )
    publish = AsyncMock(return_value="subscriber-task")
    monkeypatch.setattr(websocket_controller, "_handle_audio_chunk", publish)
    header = {
        "data": {
            "rate": 16000,
            "channels": 1,
            "width": 2,
            "captured_at_ms": 1_770_000_000_125,
            "spool_segment_id": "segment-1",
            "spool_sequence": 7,
        }
    }

    task, accepted = await websocket_controller._handle_pcm_wyoming_audio_packet(
        websocket,
        state,
        producer,
        header,
        b"pcm",
        "user-1",
        "user@example.com",
        "client-1",
    )

    assert task == "subscriber-task"
    assert accepted is True
    publish.assert_awaited_once()
    producer.flush_session_buffer.assert_awaited_once_with(
        "capture-1", sample_rate=16000, channels=1, sample_width=2
    )
    redis.set.assert_awaited_once_with(
        "mobile-audio-receipt:user-1:client-1:segment-1",
        7,
        ex=7 * 24 * 60 * 60,
    )
    websocket.send_json.assert_awaited_once_with(
        {"type": "audio-ack", "spool_segment_id": "segment-1", "sequence": 7}
    )


@pytest.mark.asyncio
async def test_pcm_duplicate_spool_packet_is_acked_without_reentering_wal(monkeypatch):
    redis = SimpleNamespace(get=AsyncMock(return_value=b"7"), set=AsyncMock())
    producer = SimpleNamespace(
        redis_client=redis,
        flush_session_buffer=AsyncMock(),
    )
    websocket = SimpleNamespace(send_json=AsyncMock())
    state = SimpleNamespace(
        stream_session_id="capture-1",
        voice_session_id=None,
        processing_profile="ambient",
    )
    publish = AsyncMock()
    monkeypatch.setattr(websocket_controller, "_handle_audio_chunk", publish)
    header = {
        "data": {
            "rate": 16000,
            "channels": 1,
            "width": 2,
            "captured_at_ms": 1_770_000_000_125,
            "spool_segment_id": "segment-1",
            "spool_sequence": 7,
        }
    }

    task, accepted = await websocket_controller._handle_pcm_wyoming_audio_packet(
        websocket,
        state,
        producer,
        header,
        b"pcm",
        "user-1",
        "user@example.com",
        "client-1",
    )

    assert task is None
    assert accepted is False
    publish.assert_not_awaited()
    producer.flush_session_buffer.assert_not_awaited()
    redis.set.assert_not_awaited()
    websocket.send_json.assert_awaited_once_with(
        {"type": "audio-ack", "spool_segment_id": "segment-1", "sequence": 7}
    )
