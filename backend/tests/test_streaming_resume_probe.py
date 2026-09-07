"""Regression tests for the streaming consumer's "did this session resume?" probe.

``transcription:complete:{session_id}`` does double duty: it stops the discovery
loop from re-attaching a second provider connection to a stream it already
finished, *and* it is the handshake ``open_conversation_job`` blocks on before
reading the final transcript. Clearing it therefore has a cost the discovery loop
cannot see — no replacement signal is ever produced, so the conversation job waits
out its full 30s timeout and finishes without the streaming result.

That is what CI run 30884816710 hit. The probe used to ask only whether the
stream's newest entry was recent, but ``finalize_session`` flushes the residual
audio and appends the end marker as its last act, so at the moment the flag is set
the newest entry is milliseconds old. A closing session looked exactly like a
resuming one, and the discovery loop cleared the flag 122ms after it was written:

    06:52:29,006  end_reason determined: websocket_disconnect
    06:52:29,128  marked complete but has fresh audio — clearing flag
    06:52:59,062  Timed out waiting for streaming completion signal (waited 30s)

The probe now decides on causal state instead: a session that has left ACTIVE can
never append again (``producer._append_owned_message`` appends inside a WATCH/MULTI
whose precondition is ``status == "active"``), and an end marker in the stream
proves the producer finished even if the FINALIZING write has not landed yet.
"""

import time

import pytest
from fakeredis import aioredis as fake_aioredis

import backend.services.transcription.streaming_consumer as sc_module
from backend.audio_contract.v2 import audio_pb2
from backend.services.audio_stream.session_store import SessionStore
from backend.services.transcription.streaming_consumer import (
    StreamingTranscriptionConsumer,
)

pytestmark = pytest.mark.unit

SESSION_ID = "989f33-plugin-tes-b43abe11e4a640f58c7f2ca8eee2aa20"
STREAM = f"audio:v2:realtime:{SESSION_ID}"


class _StubProvider:
    """The consumer resolves a provider in __init__; nothing here calls it."""

    capabilities: list[str] = []


@pytest.fixture
def consumer(monkeypatch):
    redis = fake_aioredis.FakeRedis()
    monkeypatch.setattr(
        sc_module, "get_transcription_provider", lambda mode: _StubProvider()
    )
    return StreamingTranscriptionConsumer(redis_client=redis), redis


async def _append_chunk(redis, *, age_seconds: float = 0.0, end_marker: bool = False):
    """Append one WAL entry, stamped ``age_seconds`` in the past.

    Redis stream ids are ``<ms>-<seq>`` and the probe reads the age straight off
    the id, so an explicit id makes staleness deterministic without sleeping.
    """
    entry_id = f"{int((time.time() - age_seconds) * 1000)}-*"
    binding = audio_pb2.CaptureBinding(
        capture_session_id=audio_pb2.CaptureSessionId(value=SESSION_ID)
    )
    event = audio_pb2.CaptureStreamEvent(
        frame=audio_pb2.CanonicalPcmFrame(
            binding=binding,
            delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
            pcm_s16le=b"\x00" * 8000,
        )
    )
    if end_marker:
        event = audio_pb2.CaptureStreamEvent(
            ended=audio_pb2.CaptureStreamEnded(binding=binding)
        )
    await redis.xadd(STREAM, {b"event": event.SerializeToString()}, id=entry_id)


async def _finalize_like_producer(redis, *, write_end_marker: bool = True):
    """Replay ``finalize_session``: flush residual audio, end marker, then status."""
    await _append_chunk(redis)
    if write_end_marker:
        await _append_chunk(redis, end_marker=True)
    await SessionStore(redis).mark_finalizing(SESSION_ID, "websocket_disconnect")


async def test_finalized_session_is_not_resumed_despite_a_fresh_tail(consumer):
    """The CI failure: finalize's own closing writes must not read as a resume."""
    c, redis = consumer
    await SessionStore(redis).set_status_active(SESSION_ID)
    await _finalize_like_producer(redis)

    assert await c._session_resumed(STREAM, SESSION_ID) is False


async def test_end_marker_blocks_reattach_before_the_status_write_lands(consumer):
    """The marker is appended strictly before the flag can exist, so it decides.

    ``finalize_session`` appends the marker while the session is still ACTIVE and
    only then calls ``mark_finalizing``. A consumer that reads the marker and sets
    the completion flag inside that window would otherwise see status=active.
    """
    c, redis = consumer
    await SessionStore(redis).set_status_active(SESSION_ID)
    await _append_chunk(redis)
    await _append_chunk(redis, end_marker=True)

    assert await c._session_resumed(STREAM, SESSION_ID) is False


async def test_end_marker_is_found_behind_a_late_chunk(consumer):
    """A chunk racing in behind the marker must not hide it from the tail probe."""
    c, redis = consumer
    await SessionStore(redis).set_status_active(SESSION_ID)
    await _append_chunk(redis, end_marker=True)
    await _append_chunk(redis)

    assert await c._session_resumed(STREAM, SESSION_ID) is False


async def test_finalized_session_without_an_end_marker_is_not_resumed(consumer):
    """A backend restart loses the producer buffer, so finalize writes no marker.

    Status is then the only evidence the session is over — and it is enough.
    """
    c, redis = consumer
    await SessionStore(redis).set_status_active(SESSION_ID)
    await _finalize_like_producer(redis, write_end_marker=False)

    assert await c._session_resumed(STREAM, SESSION_ID) is False


async def test_active_session_still_producing_is_resumed(consumer):
    """The self-heal this probe exists for: an idle-exited task must re-attach.

    ``process_stream`` sets the completion flag when its idle heartbeat fires, but
    the device may resume afterwards. Without re-attaching, that live stream gets
    no transcription until the flag's 5-minute TTL expires.
    """
    c, redis = consumer
    await SessionStore(redis).set_status_active(SESSION_ID)
    await _append_chunk(redis)

    assert await c._session_resumed(STREAM, SESSION_ID) is True


async def test_active_session_gone_quiet_is_not_resumed(consumer):
    """No audio for a long while — re-attaching would just churn provider sockets."""
    c, redis = consumer
    await SessionStore(redis).set_status_active(SESSION_ID)
    await _append_chunk(redis, age_seconds=60.0)

    assert await c._session_resumed(STREAM, SESSION_ID) is False


async def test_session_without_a_hash_is_not_resumed(consumer):
    """No session hash means no producer, whatever the stream still holds."""
    c, redis = consumer
    await _append_chunk(redis)

    assert await c._session_resumed(STREAM, SESSION_ID) is False


async def test_empty_stream_is_not_resumed(consumer):
    c, redis = consumer
    await SessionStore(redis).set_status_active(SESSION_ID)

    assert await c._session_resumed(STREAM, SESSION_ID) is False
