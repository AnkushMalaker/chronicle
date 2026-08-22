"""Regression tests for the streaming-transcription session-relative clock.

Streaming providers stamp word timestamps relative to their own WebSocket
session. When the provider connection drops mid-session and a new stream is
opened, the provider clock restarts at 0. Without offsetting, late audio gets
timestamps that collide with the start of the conversation and downstream
segment-building interleaves the two timelines (observed in production:
conversation e3c1dabd, 2026-06-12 — smallest.ai Pulse dropped the WS at
~20.5 min; everything transcribed after reconnect was stamped from 0 and got
bucketed into the first ~10 minutes of diarized segments).

These tests cover the consumer-level seam: chunks flow through
process_audio_chunk, the connection dies, _reconnect_session re-opens the
provider stream, and all stored results must stay on one monotonic timeline.
"""

import json
from types import SimpleNamespace

import pytest
from fakeredis import aioredis as fake_aioredis

import advanced_omi_backend.services.transcription.streaming_consumer as sc_module
from advanced_omi_backend.redis_keys import SessionId, audio_session
from advanced_omi_backend.services.transcription.streaming_consumer import (
    StreamingTranscriptionConsumer,
    _apply_time_offset,
)

pytestmark = pytest.mark.unit

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # PCM 16-bit mono


# --------------------------------------------------------------------------- #
# _apply_time_offset
# --------------------------------------------------------------------------- #


def test_apply_time_offset_shifts_words_and_segments():
    result = {
        "words": [{"word": "hi", "start": 0.5, "end": 1.0}],
        "segments": [
            {
                "start": 0.5,
                "end": 1.0,
                "text": "hi",
                "words": [{"word": "hi", "start": 0.5, "end": 1.0}],
            }
        ],
    }
    _apply_time_offset(result, 100.0)

    assert result["words"][0]["start"] == pytest.approx(100.5)
    assert result["words"][0]["end"] == pytest.approx(101.0)
    assert result["segments"][0]["start"] == pytest.approx(100.5)
    assert result["segments"][0]["end"] == pytest.approx(101.0)
    assert result["segments"][0]["words"][0]["start"] == pytest.approx(100.5)


def test_apply_time_offset_zero_is_noop_and_tolerates_missing_fields():
    result = {"words": [{"word": "hi", "start": 0.5, "end": 1.0}, {"word": "x"}]}
    _apply_time_offset(result, 0.0)
    assert result["words"][0]["start"] == pytest.approx(0.5)

    # Words without timestamps must not raise
    _apply_time_offset(result, 10.0)
    assert result["words"][0]["start"] == pytest.approx(10.5)
    assert "start" not in result["words"][1]


# --------------------------------------------------------------------------- #
# Consumer seam: provider clock reset across reconnect
# --------------------------------------------------------------------------- #


class FakeStreamingProvider:
    """Minimal provider whose word clock is relative to each start_stream call,
    mirroring real streaming providers (Pulse, Deepgram)."""

    capabilities: set = set()
    # ``name`` and ``mode`` are abstract on BaseTranscriptionProvider and are what
    # transcript provenance is written from. A double that omits them lets the
    # consumer record a provider it never had.
    name: str = "fake-streaming-provider"
    mode: str = "streaming"

    def __init__(self):
        self.clock = 0.0
        self.sessions_started = 0
        self.dead = False

    async def start_stream(self, stream_id, sample_rate=16000, diarize=False):
        self.sessions_started += 1
        self.clock = 0.0  # the bug trigger: every new WS session restarts at 0
        self.dead = False

    async def process_audio_chunk(self, stream_id, audio_chunk):
        if self.dead:
            raise ConnectionError("provider socket closed")
        secs = len(audio_chunk) / BYTES_PER_SECOND
        start = self.clock
        self.clock += secs
        return {
            "text": "word",
            "words": [
                {"word": "word", "start": start, "end": self.clock, "confidence": 1.0}
            ],
            "segments": [],
            "is_final": True,
            "confidence": 1.0,
        }

    async def end_stream(self, stream_id):
        if self.dead:
            raise ConnectionError("provider socket closed")
        return {"text": "", "words": [], "segments": []}


@pytest.fixture
def consumer(monkeypatch):
    redis = fake_aioredis.FakeRedis()
    provider = FakeStreamingProvider()
    monkeypatch.setattr(sc_module, "get_transcription_provider", lambda mode: provider)
    c = StreamingTranscriptionConsumer(redis_client=redis)
    return c, provider, redis


async def _stored_word_starts(redis, session_id):
    entries = await redis.xrange(f"transcription:results:{session_id}")
    words = []
    for _, fields in entries:
        words.extend(json.loads(fields[b"words"]))
    return [w["start"] for w in words]


async def _write_session_identity(
    redis, session_id: str, client_id: str, user_id: str = "user-1"
) -> None:
    await redis.hset(
        audio_session(SessionId.from_value(session_id)),
        mapping={"user_id": user_id, "client_id": client_id},
    )


async def test_timestamps_stay_monotonic_across_reconnect(consumer):
    c, provider, redis = consumer
    session_id = "a421c9-havpe-test"
    one_second_chunk = b"\x00" * BYTES_PER_SECOND

    await c.start_session_stream(session_id, sample_rate=SAMPLE_RATE)
    for i in range(3):
        await c.process_audio_chunk(session_id, one_second_chunk, f"pre-{i}")

    # Provider kills the connection (e.g. max WS session duration)
    provider.dead = True
    with pytest.raises(ConnectionError):
        await c.process_audio_chunk(session_id, one_second_chunk, "dying")

    assert await c._reconnect_session(session_id)
    assert provider.sessions_started == 2  # fresh provider session, clock back at 0

    for i in range(2):
        await c.process_audio_chunk(session_id, one_second_chunk, f"post-{i}")

    starts = await _stored_word_starts(redis, session_id)
    assert starts == sorted(starts), "word timeline must stay monotonic"
    # 3s sent before the drop (the dying chunk never reached the provider),
    # so the post-reconnect words must continue at +3s — not restart at 0.
    assert starts[3] == pytest.approx(3.0)
    assert starts[4] == pytest.approx(4.0)


async def test_clock_survives_consumer_restart_via_session_store(consumer, monkeypatch):
    """The offset also resumes from the session hash when the consumer process
    itself restarts (in-memory counter lost)."""
    c, provider, redis = consumer
    session_id = "a421c9-havpe-test2"
    one_second_chunk = b"\x00" * BYTES_PER_SECOND

    await c.start_session_stream(session_id, sample_rate=SAMPLE_RATE)
    for i in range(3):
        await c.process_audio_chunk(session_id, one_second_chunk, f"pre-{i}")
    # end_session_stream persists the clock to the session hash
    await c.end_session_stream(session_id)
    assert session_id not in c._session_audio_seconds

    # New consumer instance (same Redis) — e.g. workers container restart
    monkeypatch.setattr(sc_module, "get_transcription_provider", lambda mode: provider)
    c2 = StreamingTranscriptionConsumer(redis_client=redis)
    await c2.start_session_stream(session_id, sample_rate=SAMPLE_RATE)
    await c2.process_audio_chunk(session_id, one_second_chunk, "post-0")

    starts = await _stored_word_starts(redis, session_id)
    assert starts[-1] == pytest.approx(3.0)


async def test_streaming_followup_uses_session_metadata_client_id(
    consumer, monkeypatch
):
    c, _provider, redis = consumer
    session_id = "session-uuid"
    client_id = "a421c9-elato"
    seen = {}

    await _write_session_identity(redis, session_id, client_id)

    async def capture_followup(
        redis_client, plugin_router, *, user_id, session_id, client_id, text
    ):
        seen.update(
            {
                "user_id": user_id,
                "session_id": session_id,
                "client_id": client_id,
                "text": text,
            }
        )
        return True

    monkeypatch.setattr(sc_module, "maybe_handle_followup", capture_followup)
    c.plugin_router = SimpleNamespace()

    await c.trigger_plugins(session_id, {"text": "make it warmer", "words": []})

    assert seen == {
        "user_id": "user-1",
        "session_id": session_id,
        "client_id": client_id,
        "text": "make it warmer",
    }


async def test_streaming_plugin_dispatch_uses_session_metadata_client_id(
    consumer, monkeypatch
):
    c, _provider, redis = consumer
    session_id = "session-uuid"
    client_id = "a421c9-elato"
    seen = {}

    await _write_session_identity(redis, session_id, client_id)

    async def ignore_followup(
        redis_client, plugin_router, *, user_id, session_id, client_id, text
    ):
        return False

    class CapturingRouter:
        async def dispatch_event(self, *, event, user_id, data, metadata):
            seen.update(
                {
                    "event": event,
                    "user_id": user_id,
                    "data": data,
                    "metadata": metadata,
                }
            )
            return [{"ok": True}]

    monkeypatch.setattr(sc_module, "maybe_handle_followup", ignore_followup)
    c.plugin_router = CapturingRouter()

    await c.trigger_plugins(
        session_id,
        {
            "text": "please summarize",
            "words": [],
            "segments": [],
            "confidence": 0.75,
        },
    )

    assert seen["event"] == sc_module.PluginEvent.TRANSCRIPT_STREAMING
    assert seen["user_id"] == "user-1"
    assert seen["data"]["session_id"] == session_id
    assert seen["data"]["client_id"] == client_id
    assert seen["metadata"] == {"client_id": client_id, "session_id": session_id}


async def test_speaker_identification_uses_session_metadata_user_id(consumer):
    c, _provider, redis = consumer
    session_id = "session-uuid"
    client_id = "a421c9-elato"
    seen = {}

    await _write_session_identity(redis, session_id, client_id)
    c._audio_buffers[session_id] = bytearray(b"\x00" * 3200)

    async def identify_segment(*, audio_wav_bytes, user_id):
        seen["user_id"] = user_id
        seen["audio_wav_bytes"] = audio_wav_bytes
        return {"found": True, "speaker_name": "Alex", "confidence": 0.9}

    c.speaker_client = SimpleNamespace(enabled=True, identify_segment=identify_segment)

    speaker_name, confidence = await c._identify_speaker(session_id)

    assert speaker_name == "Alex"
    assert confidence == pytest.approx(0.9)
    assert seen["user_id"] == "user-1"
    assert seen["audio_wav_bytes"].startswith(b"RIFF")


@pytest.mark.asyncio
async def test_stored_results_record_the_provider_not_the_processing_mode(consumer):
    """``streaming`` is how a transcript was produced, not who produced it.

    The consumer used to write ``provider=b"streaming"``; the aggregator read that
    field as the provider and conversation persistence stored it as the transcript
    version's provider *and* its model, so which service actually transcribed the
    audio was unrecoverable afterwards.
    """

    c, provider, redis = consumer
    session_id = "a421c9-havpe-provenance"

    await c.start_session_stream(session_id, sample_rate=SAMPLE_RATE)
    await c.process_audio_chunk(session_id, b"\x00" * BYTES_PER_SECOND, "chunk-0")

    entries = await redis.xrange(f"transcription:results:{session_id}")
    assert entries, "expected a stored final result"
    fields = entries[-1][1]

    assert fields[b"provider"].decode() == "fake-streaming-provider"
    assert fields[b"mode"].decode() == "streaming"
    # The mode must not be able to masquerade as the provider again.
    assert fields[b"provider"] != b"streaming"
    assert b"model" in fields
