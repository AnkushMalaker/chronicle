import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from active_turn_consumer import COMMITTED_TURNS_STREAM, ActiveTurnConsumer


class FakeRedis:
    def __init__(self):
        self.added = []

    async def xadd(self, stream, fields, **kwargs):
        self.added.append((stream, fields, kwargs))
        return b"1-0"


class FakeModels:
    async def evaluate(self, pcm):
        speech = pcm.startswith(b"speech")
        return speech, not speech


class FakeClock:
    def __init__(self):
        self.now_ms = 10_000.0

    def __call__(self):
        return self.now_ms


def _fields(sequence: int, pcm: bytes) -> dict:
    return {
        b"voice_session_id": b"voice-1",
        b"audio_session_id": b"audio-1",
        b"capture_epoch": b"2",
        b"frame_sequence": str(sequence).encode(),
        b"monotonic_offset_ms": str(sequence * 40).encode(),
        b"sample_rate": b"16000",
        b"sample_count": b"640",
        b"pcm": pcm,
    }


@pytest.mark.asyncio
async def test_active_consumer_publishes_only_committed_turns():
    redis = FakeRedis()
    clock = FakeClock()
    consumer = ActiveTurnConsumer(
        redis_client=redis,
        model_factory=FakeModels,
        monotonic_ms=clock,
    )
    for sequence, pcm in enumerate(
        [b"speech-1", b"speech-2", b"silence", b"silence", b"silence"]
    ):
        await consumer.handle_frame(_fields(sequence, pcm))

    assert all(stream != COMMITTED_TURNS_STREAM for stream, _, _ in redis.added)
    clock.now_ms += 2_000
    await consumer.flush_due()

    committed = [
        fields for stream, fields, _ in redis.added if stream == COMMITTED_TURNS_STREAM
    ]
    assert len(committed) == 1
    assert committed[0]["voice_session_id"] == "voice-1"
    assert committed[0]["audio_session_id"] == "audio-1"
    assert committed[0]["turn_revision"] == "0"
    assert committed[0]["pcm"].startswith(b"speech")


@pytest.mark.asyncio
async def test_consumer_health_records_fresh_success_and_errors():
    redis = FakeRedis()
    consumer = ActiveTurnConsumer(redis_client=redis, model_factory=FakeModels)

    await consumer.handle_frame(_fields(0, b"speech"))
    with pytest.raises(ValueError):
        await consumer.handle_frame({b"voice_session_id": b"voice-1"})

    health = consumer.health()
    assert health["frames_consumed"] == 1
    assert health["error_count"] == 1
    assert health["last_success_at"] is not None


@pytest.mark.asyncio
async def test_consumer_recovers_own_and_stranded_peer_frames_before_acknowledging():
    own = (b"10-0", _fields(10, b"speech-own"))
    peer = (b"11-0", _fields(11, b"speech-peer"))
    redis = SimpleNamespace(
        xreadgroup=AsyncMock(side_effect=[[(b"voice:frames:voice-1", [own])], []]),
        xautoclaim=AsyncMock(return_value=(b"0-0", [peer], [])),
        xack=AsyncMock(),
    )
    consumer = ActiveTurnConsumer(redis_client=redis, model_factory=FakeModels)
    consumer.handle_frame = AsyncMock()

    recovered = await consumer.recover_pending(
        "voice:frames:voice-1",
        claim_min_idle_ms=0,
    )

    assert recovered == 2
    assert consumer.handle_frame.await_count == 2
    assert redis.xack.await_count == 2
    redis.xautoclaim.assert_awaited_once_with(
        "voice:frames:voice-1",
        "active_turns",
        consumer.consumer_name,
        0,
        start_id="0-0",
        count=20,
    )


@pytest.mark.asyncio
async def test_failed_active_frame_remains_pending_for_retry():
    entry = (b"12-0", _fields(12, b"speech"))
    redis = SimpleNamespace(
        xreadgroup=AsyncMock(return_value=[(b"voice:frames:voice-1", [entry])]),
        xautoclaim=AsyncMock(),
        xack=AsyncMock(),
    )
    consumer = ActiveTurnConsumer(redis_client=redis, model_factory=FakeModels)
    consumer.handle_frame = AsyncMock(side_effect=RuntimeError("model reset"))

    recovered = await consumer.recover_pending("voice:frames:voice-1")

    assert recovered == 0
    redis.xack.assert_not_awaited()
    redis.xautoclaim.assert_not_awaited()
