"""Unit tests for the SessionStore facade and SessionView read-model.

Covers the two things the facade exists to guarantee:
1. Decoding is identical whether the redis client returns bytes or str.
2. Lifecycle writes are atomic (single hset) and the signal publish follows the
   hash write.
"""

import json

import pytest
from fakeredis import aioredis as fake_aioredis

from backend.services.audio_stream.session_store import SessionStatus
from backend.services.audio_stream.session_store import (
    SessionStore as ProductionSessionStore,
)
from backend.services.audio_stream.session_store import SessionView, SpeakerCheckStatus

pytestmark = pytest.mark.unit


class SessionStore(ProductionSessionStore):
    """Ambient provenance fixture for tests that exercise unrelated store behavior."""

    async def init_session(self, session_id: str, **kwargs) -> None:
        kwargs.update(
            capture_epoch=0,
            processing_profile="ambient",
            effects={
                "aec": {"reporting": "unreported"},
                "noise_suppression": {"reporting": "unreported"},
            },
            voice_session_id=None,
        )
        await super().init_session(session_id, **kwargs)


async def test_production_session_init_requires_capture_provenance():
    store = ProductionSessionStore(_fake_redis())

    with pytest.raises(TypeError):
        await store.init_session(
            "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
        )


async def test_session_memory_space_survives_redis_round_trip():
    store = ProductionSessionStore(_fake_redis())
    await store.init_session(
        "sess-space",
        user_id="u1",
        client_id="c1",
        stream_name="audio:stream:c1",
        capture_epoch=0,
        processing_profile="source_native",
        effects={
            "aec": {"reporting": "unreported"},
            "noise_suppression": {"reporting": "unreported"},
        },
        voice_session_id=None,
        memory_space_id="9f3523c8-af75-469d-995a-7179531f3fc8",
    )

    view = await store.read("sess-space")

    assert view is not None
    assert view.memory_space_id == "9f3523c8-af75-469d-995a-7179531f3fc8"


def _fake_redis(decode_responses=False):
    return fake_aioredis.FakeRedis(decode_responses=decode_responses)


# --------------------------------------------------------------------------- #
# SessionView.from_hash
# --------------------------------------------------------------------------- #


def _sample_str_hash():
    return {
        "user_id": "507f1f77bcf86cd799439011",
        "client_id": "a39011-phone",
        "status": "finalizing",
        "websocket_connected": "true",
        "chunks_published": "42",
        "started_at": "1704067200.5",
        "last_chunk_at": "1704067260.0",
        "finalized_at": "1704067300.0",
        "speaker_check_status": "enrolled",
        "audio_format": json.dumps({"rate": 48000, "channels": 2, "width": 2}),
        "markers": json.dumps([{"type": "button_event"}]),
        "identified_speakers": "Alice,Bob",
        "speech_detected_at": "2026-01-01T00:00:00+00:00",
        "completion_reason": "user_stopped",
    }


def test_from_hash_bytes_and_str_are_identical():
    str_hash = _sample_str_hash()
    bytes_hash = {k.encode(): v.encode() for k, v in str_hash.items()}

    view_from_str = SessionView.from_hash("sess-1", str_hash)
    view_from_bytes = SessionView.from_hash("sess-1", bytes_hash)

    assert view_from_str == view_from_bytes


def test_from_hash_coercions():
    view = SessionView.from_hash("sess-1", _sample_str_hash())

    assert view.status is SessionStatus.FINALIZING
    assert view.speaker_check_status is SpeakerCheckStatus.ENROLLED
    assert view.websocket_connected is True
    assert view.chunks_published == 42
    assert view.started_at == 1704067200.5
    assert view.finalized_at == 1704067300.0
    assert view.completed_at is None  # missing -> None (not 0.0)
    assert view.audio_format == {"rate": 48000, "channels": 2, "width": 2}
    assert view.audio_format_tuple == (48000, 2, 2)
    assert view.markers == [{"type": "button_event"}]
    assert view.identified_speakers == ["Alice", "Bob"]
    assert view.speech_detected_at == "2026-01-01T00:00:00+00:00"


def test_from_hash_unknown_enum_and_empty_defaults():
    view = SessionView.from_hash(
        "sess-1",
        {"status": "bogus", "chunks_published": "", "websocket_connected": "false"},
    )
    assert view.status is None  # unknown enum value -> None, not a raise
    assert view.chunks_published == 0
    assert view.websocket_connected is False
    assert view.started_at == 0.0
    assert view.audio_format is None
    assert view.markers == []
    assert view.identified_speakers == []


def test_from_hash_malformed_audio_format_falls_back():
    view = SessionView.from_hash("sess-1", {"audio_format": "not-json"})
    assert view.audio_format is None
    assert view.audio_format_tuple == (16000, 1, 2)


# --------------------------------------------------------------------------- #
# SessionStore lifecycle writes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("decode_responses", [False, True])
async def test_init_session_writes_expected_mapping(decode_responses):
    client = _fake_redis(decode_responses)
    store = SessionStore(client)

    await store.init_session(
        "sess-1",
        user_id="u1",
        client_id="c1",
        stream_name="audio:stream:c1",
        user_email="e@x.com",
        mode="streaming",
        provider="deepgram",
    )

    view = await store.read("sess-1")
    assert view is not None
    assert view.user_id == "u1"
    assert view.client_id == "c1"
    assert view.stream_name == "audio:stream:c1"
    assert view.user_email == "e@x.com"
    assert view.provider == "deepgram"
    assert view.mode == "streaming"
    assert view.status is SessionStatus.ACTIVE
    assert view.websocket_connected is True
    assert view.chunks_published == 0
    assert view.started_at > 0


async def test_mark_finalizing_sets_status_and_publishes_signal():
    client = _fake_redis()
    store = SessionStore(client)
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )

    pubsub = client.pubsub()
    await pubsub.subscribe("session:signal:sess-1")
    await pubsub.get_message(timeout=1)  # consume subscribe ack

    await store.mark_finalizing("sess-1", "websocket_disconnect")

    view = await store.read("sess-1")
    assert view.status is SessionStatus.FINALIZING
    assert view.finalized_at is not None
    assert view.completion_reason == "websocket_disconnect"
    assert view.websocket_connected is False  # set on websocket_disconnect

    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    assert msg is not None
    payload = json.loads(msg["data"])
    assert payload == {"type": "finalize", "reason": "websocket_disconnect"}


async def test_mark_complete_atomic_hset_then_publish():
    """status + completed_at + completion_reason in one hset, publish after."""

    class Recorder:
        def __init__(self, inner):
            self._inner = inner
            self.calls = []

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if name in ("hset", "publish"):

                async def wrapper(*a, **k):
                    self.calls.append(name)
                    return await attr(*a, **k)

                return wrapper
            return attr

    rec = Recorder(_fake_redis())
    store = SessionStore(rec)
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )
    rec.calls.clear()

    await store.mark_complete("sess-1", "user_stopped")

    # exactly one hset, then the publish
    assert rec.calls == ["hset", "publish"]
    view = await store.read("sess-1")
    assert view.status is SessionStatus.FINISHED
    assert view.completion_reason == "user_stopped"
    assert view.completed_at is not None


async def test_take_close_request_returns_then_clears():
    client = _fake_redis()
    store = SessionStore(client)
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )
    await store.set_active_conversation("sess-1", "conversation-1")
    assert await store.request_close("sess-1", "user_requested") is True

    assert await store.take_close_request("sess-1") == "user_requested"
    assert await store.take_close_request("sess-1") is None  # consumed


async def test_request_close_returns_false_when_missing():
    store = SessionStore(_fake_redis())
    assert await store.request_close("nope", "user_requested") is False


async def test_request_close_returns_false_before_speech_materializes_conversation():
    store = SessionStore(_fake_redis())
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )

    assert await store.request_close("sess-1", "user_requested") is False


async def test_get_audio_format_fallback_on_missing_and_garbage():
    client = _fake_redis()
    store = SessionStore(client)
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )

    assert await store.get_audio_format("sess-1") == (16000, 1, 2)  # not set yet

    await client.hset("audio:session:sess-1", "audio_format", "not-json")
    assert await store.get_audio_format("sess-1") == (16000, 1, 2)  # garbage -> default

    await store.set_audio_format("sess-1", {"rate": 8000, "channels": 1, "width": 2})
    assert await store.get_audio_format("sess-1") == (8000, 1, 2)


async def test_bump_chunk_count_increments():
    store = SessionStore(_fake_redis())
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )
    await store.bump_chunk_count("sess-1")
    await store.bump_chunk_count("sess-1")
    view = await store.read("sess-1")
    assert view.chunks_published == 2
    assert view.last_chunk_at > 0


async def test_transcription_provider_health_lifecycle():
    store = SessionStore(_fake_redis())
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )

    initial = await store.read("sess-1")
    assert initial.transcription_provider_status == "disconnected"

    await store.mark_transcription_provider_connected("sess-1")
    await store.mark_transcription_audio_sent("sess-1")
    await store.mark_transcription_provider_message("sess-1")

    connected = await store.read("sess-1")
    assert connected.transcription_provider_status == "connected"
    assert connected.transcription_provider_connected_at > 0
    assert connected.transcription_last_audio_sent_at > 0
    assert connected.transcription_last_message_at > 0

    await store.set_transcription_error("sess-1", "socket closed")
    failed = await store.read("sess-1")
    assert failed.transcription_provider_status == "error"
    assert failed.transcription_error == "socket closed"

    await store.mark_transcription_provider_disconnected("sess-1")
    still_failed = await store.read("sess-1")
    assert still_failed.transcription_provider_status == "error"

    await store.mark_transcription_provider_connected("sess-1")
    await store.mark_transcription_provider_disconnected("sess-1")
    disconnected = await store.read("sess-1")
    assert disconnected.transcription_provider_status == "disconnected"


async def test_get_status_ws_reason_batched():
    store = SessionStore(_fake_redis())
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )
    await store.mark_complete("sess-1", "inactivity_timeout")
    status, ws, reason = await store.get_status_ws_reason("sess-1")
    assert status is SessionStatus.FINISHED
    # inactivity_timeout does NOT clear websocket_connected (only websocket_disconnect does)
    assert ws is True
    assert reason == "inactivity_timeout"


async def test_iter_views_strips_prefix_and_skips_empty():
    client = _fake_redis()
    store = SessionStore(client)
    await store.init_session(
        "alpha", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )
    await store.init_session(
        "beta", user_id="u2", client_id="c2", stream_name="audio:stream:c2"
    )
    # an empty hash key (e.g. fully hdel'd) should be skipped by iter_views
    await client.hset("audio:session:ghost", "x", "1")
    await client.hdel("audio:session:ghost", "x")

    ids = sorted([sid async for sid in store.scan_session_ids()])
    assert "alpha" in ids and "beta" in ids

    views = {v.session_id: v async for v in store.iter_views()}
    assert set(views) == {"alpha", "beta"}  # ghost skipped
    assert views["alpha"].user_id == "u1"


async def test_increment_conversation_count_sets_ttl():
    client = _fake_redis()
    store = SessionStore(client)
    assert await store.get_conversation_count("sess-1") == 0
    assert await store.increment_conversation_count("sess-1") == 1
    assert await store.increment_conversation_count("sess-1") == 2
    assert await store.get_conversation_count("sess-1") == 2
    ttl = await client.ttl("session:conversation_count:sess-1")
    assert 0 < ttl <= 3600


async def test_record_event_and_speaker_check():
    store = SessionStore(_fake_redis())
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:c1"
    )
    await store.record_event("sess-1", "speech_detected")
    await store.set_speaker_check("sess-1", SpeakerCheckStatus.ENROLLED)
    await store.set_identified_speakers("sess-1", ["Alice", "Bob"])

    view = await store.read("sess-1")
    assert view.last_event.startswith("speech_detected:")
    assert view.speaker_check_status is SpeakerCheckStatus.ENROLLED
    assert view.identified_speakers == ["Alice", "Bob"]


# --------------------------------------------------------------------------- #
# Active semantic Conversation (a field on the session hash)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("decode_responses", [False, True])
async def test_active_conversation_lives_on_active_session_hash(decode_responses):
    client = _fake_redis(decode_responses)
    store = SessionStore(client)
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:sess-1"
    )

    assert await store.set_active_conversation("sess-1", "conv-A") is True
    assert await store.get_active_conversation_id("sess-1") == "conv-A"
    view = await store.read("sess-1")
    assert view.active_conversation_id == "conv-A"
    assert await client.exists("conversation:current:sess-1") == 0


async def test_active_conversation_cannot_replace_a_different_open_claim():
    store = SessionStore(_fake_redis())
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:sess-1"
    )

    assert await store.set_active_conversation("sess-1", "conv-A") is True
    assert await store.set_active_conversation("sess-1", "conv-B") is False
    assert await store.get_active_conversation_id("sess-1") == "conv-A"


async def test_active_conversation_rejects_terminal_or_missing_session():
    store = SessionStore(_fake_redis())

    assert await store.set_active_conversation("missing", "conv-A") is False
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:sess-1"
    )
    await store.mark_finalizing("sess-1", "websocket_disconnect")
    assert await store.set_active_conversation("sess-1", "conv-A") is False
    assert await store.get_active_conversation_id("sess-1") is None


async def test_clear_active_conversation_preserves_a_newer_claim():
    store = SessionStore(_fake_redis())
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:sess-1"
    )
    await store.set_active_conversation("sess-1", "conv-B")

    cleared = await store.clear_active_conversation("sess-1", expected_id="conv-A")

    assert cleared is False
    assert await store.get_active_conversation_id("sess-1") == "conv-B"


async def test_clear_active_conversation_removes_expected_claim():
    store = SessionStore(_fake_redis())
    await store.init_session(
        "sess-1", user_id="u1", client_id="c1", stream_name="audio:stream:sess-1"
    )
    await store.set_active_conversation("sess-1", "conv-A")

    cleared = await store.clear_active_conversation("sess-1", expected_id="conv-A")

    assert cleared is True
    assert await store.get_active_conversation_id("sess-1") is None
