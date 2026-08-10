"""Tests for recovering audio a stream consumer was handed but never acknowledged.

The failure these pin, reproduced from a live stream:

A consumer buffers chunks in memory and commits them when the window fills or the
end-of-session marker arrives. The buffer is process-local; the record of what was
delivered is Redis's pending entries list. When a worker stopped mid-window, the
buffer vanished and the pending entries stayed — and because the read loop only ever
asked for ``">"`` (undelivered messages), no later incarnation ever looked at them.

The observed result was 119 of a 120-chunk window stranded: 30 seconds of audio never
transcribed, and a 140 MB write-ahead log that could never be reclaimed because its
consumer group never drained. The END marker had been delivered and acknowledged —
the flush ran and found nothing to flush.
"""

import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.services.audio_stream import consumer as consumer_module
from advanced_omi_backend.services.audio_stream.consumer import BaseAudioStreamConsumer
from advanced_omi_backend.services.audio_stream.durability import (
    inspect_stream_retention,
)

pytestmark = pytest.mark.unit

WINDOW_CHUNKS = 120
SAMPLE_RATE = 16000
# 0.25s of 16-bit mono PCM, matching the producer's fixed chunk size.
CHUNK = b"\x00" * (SAMPLE_RATE // 4 * 2)


class _RecordingConsumer(BaseAudioStreamConsumer):
    """A consumer whose transcription is recorded rather than performed."""

    def __init__(self, redis_client, buffer_chunks=WINDOW_CHUNKS):
        super().__init__(
            provider_name="windowed-batch",
            redis_client=redis_client,
            buffer_chunks=buffer_chunks,
        )
        self.transcribed: list = []
        self.stored: list = []

    async def transcribe_audio(self, audio_data: bytes, sample_rate: int) -> dict:
        self.transcribed.append(len(audio_data))
        return {
            "text": f"window of {len(audio_data)} bytes",
            "confidence": 0.9,
            "words": [{"word": "hi", "start": 0.0, "end": 0.5}],
            "segments": [{"start": 0.0, "end": 0.5, "text": "hi"}],
        }

    async def store_result(self, **kwargs):
        self.stored.append(kwargs)


async def _publish(redis, stream, session_id, count, *, end=False):
    for i in range(count):
        await redis.xadd(
            stream,
            {
                b"audio_data": CHUNK,
                b"session_id": session_id.encode(),
                b"chunk_id": f"c{i}".encode(),
                b"sample_rate": str(SAMPLE_RATE).encode(),
            },
        )
    if end:
        await redis.xadd(
            stream,
            {
                b"audio_data": b"",
                b"session_id": session_id.encode(),
                b"chunk_id": b"END",
                b"sample_rate": str(SAMPLE_RATE).encode(),
                b"end_marker": b"true",
            },
        )


async def _deliver_without_acking(redis, stream, group, consumer_name, count):
    """Hand entries to a consumer that then disappears without acknowledging."""
    await redis.xgroup_create(stream, group, id="0")
    await redis.xreadgroup(group, consumer_name, {stream: ">"}, count=count)


async def _pending(redis, stream, group) -> int:
    groups = await redis.execute_command("XINFO", "GROUPS", stream)
    values = {groups[0][i]: groups[0][i + 1] for i in range(0, len(groups[0]), 2)}
    return int(values[b"pending"])


@pytest.fixture
def redis_client():
    return fake_aioredis.FakeRedis()


@pytest.fixture(autouse=True)
def claim_immediately(monkeypatch):
    """Entries cannot be aged in fakeredis, so claim regardless of idle time.

    The threshold itself is asserted separately, in
    ``test_a_window_another_worker_is_still_filling_is_not_claimed``.
    """
    monkeypatch.setattr(consumer_module, "PENDING_CLAIM_MIN_IDLE_SECONDS", 0)


# --------------------------------------------------------------------------- #
# The stranded partial window
# --------------------------------------------------------------------------- #


async def test_a_partial_window_left_by_a_dead_worker_is_recovered(redis_client):
    """The exact live failure: 119 of a 120-chunk window, stranded."""
    stream, session = "audio:stream:s1", "s1"
    await _publish(redis_client, stream, session, 119)
    consumer = _RecordingConsumer(redis_client)
    await _deliver_without_acking(
        redis_client, stream, consumer.group_name, "windowed-batch-worker-old", 119
    )
    assert await _pending(redis_client, stream, consumer.group_name) == 119

    claimed = await consumer.recover_pending(stream)

    assert claimed == 119
    # The audio is transcribed rather than silently dropped...
    assert len(consumer.transcribed) == 1
    assert consumer.transcribed[0] == 119 * len(CHUNK)
    # ...and the group drains, which is what lets the stream be reclaimed.
    assert await _pending(redis_client, stream, consumer.group_name) == 0


async def test_recovery_drains_the_group_so_the_log_can_be_reclaimed(redis_client):
    stream, session = "audio:stream:s1", "s1"
    await _publish(redis_client, stream, session, 40)
    consumer = _RecordingConsumer(redis_client)
    await _deliver_without_acking(
        redis_client, stream, consumer.group_name, "dead-worker", 40
    )

    await consumer.recover_pending(stream)

    decision = await inspect_stream_retention(
        redis_client, stream, required_groups={consumer.group_name}
    )
    assert decision.safe_to_delete is True


async def test_recovery_is_a_no_op_when_nothing_is_pending(redis_client):
    stream = "audio:stream:s1"
    await _publish(redis_client, stream, "s1", 5)
    consumer = _RecordingConsumer(redis_client)
    await redis_client.xgroup_create(stream, consumer.group_name, id="0")

    claimed = await consumer.recover_pending(stream)

    assert claimed == 0
    assert consumer.transcribed == []


async def test_an_end_marker_among_recovered_entries_still_settles(redis_client):
    """A worker that died after the marker was written must still commit."""
    stream, session = "audio:stream:s1", "s1"
    await _publish(redis_client, stream, session, 10, end=True)
    consumer = _RecordingConsumer(redis_client)
    await _deliver_without_acking(
        redis_client, stream, consumer.group_name, "dead-worker", 11
    )

    claimed = await consumer.recover_pending(stream)

    assert claimed == 11
    assert await _pending(redis_client, stream, consumer.group_name) == 0
    assert session not in consumer.session_buffers


# --------------------------------------------------------------------------- #
# What recovery must not do
# --------------------------------------------------------------------------- #


async def test_a_window_another_worker_is_still_filling_is_not_claimed(
    redis_client, monkeypatch
):
    """Claiming live work would transcribe the same audio twice.

    A consumer holds a partial window unacknowledged for as long as it takes to
    fill, so recovery only claims entries idle beyond the threshold.
    """
    monkeypatch.setattr(consumer_module, "PENDING_CLAIM_MIN_IDLE_SECONDS", 300)
    stream, session = "audio:stream:s1", "s1"
    await _publish(redis_client, stream, session, 60)
    consumer = _RecordingConsumer(redis_client)
    await _deliver_without_acking(
        redis_client, stream, consumer.group_name, "busy-sibling", 60
    )

    claimed = await consumer.recover_pending(stream)

    assert claimed == 0
    assert consumer.transcribed == []
    assert await _pending(redis_client, stream, consumer.group_name) == 60


async def test_audio_is_acknowledged_only_after_the_result_is_stored(redis_client):
    """A failed transcription must leave the audio recoverable, not consumed."""
    stream, session = "audio:stream:s1", "s1"
    await _publish(redis_client, stream, session, 30)
    consumer = _RecordingConsumer(redis_client)
    await _deliver_without_acking(
        redis_client, stream, consumer.group_name, "dead-worker", 30
    )

    async def _fail(audio_data, sample_rate):
        raise RuntimeError("provider unavailable")

    consumer.transcribe_audio = _fail

    await consumer.recover_pending(stream)

    assert consumer.stored == []
    assert await _pending(redis_client, stream, consumer.group_name) == 30


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


async def test_a_flushed_window_is_placed_on_the_session_clock(redis_client):
    """Every flush offsets timestamps, including the final partial one.

    The end-of-session flush used to store provider timestamps unshifted, so the
    last window's words restarted from zero and collided with the opening of the
    conversation.
    """
    stream, session = "audio:stream:s1", "s1"
    consumer = _RecordingConsumer(redis_client, buffer_chunks=4)
    await redis_client.xgroup_create(stream, consumer.group_name, id="0", mkstream=True)
    await _publish(redis_client, stream, session, 8, end=True)
    delivered = await redis_client.xreadgroup(
        consumer.group_name, consumer.consumer_name, {stream: ">"}, count=20
    )
    for _, msgs in delivered:
        for message_id, fields in msgs:
            await consumer.process_message(message_id, fields, stream)

    # Two full windows plus the end-of-session flush of the remainder.
    assert len(consumer.stored) >= 2
    offsets = [s["words"][0]["start"] for s in consumer.stored]
    assert offsets[0] == 0.0
    assert offsets[1] > 0.0
    assert offsets == sorted(offsets)
