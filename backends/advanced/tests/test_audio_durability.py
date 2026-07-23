"""Durability invariants from Redis acceptance through Mongo persistence."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.services.audio_stream.durability import (
    PersistenceOutcome,
    PersistenceRuntimeState,
    ReadPhase,
    SessionPhase,
)
from advanced_omi_backend.services.audio_stream.producer import (
    AudioStreamProducer,
    SessionBuffer,
)
from advanced_omi_backend.services.audio_stream.session_store import SessionStatus
from advanced_omi_backend.services.transcription.streaming_consumer import (
    StreamingTranscriptionConsumer,
)
from advanced_omi_backend.workers import audio_jobs, transcription_jobs

pytestmark = pytest.mark.unit


class _QueryField:
    def __eq__(self, value):
        return value


class _DurabilityStore:
    def __init__(self, redis_client):
        self._statuses = iter(redis_client.statuses)

    async def get_current_conversation_id(self, session_id):
        return "conversation-1"

    async def get_audio_format(self, session_id):
        return 16000, 1, 2

    async def get_last_chunk_at(self, session_id):
        return time.time()

    async def is_websocket_connected(self, session_id):
        return True

    async def get_status(self, session_id):
        return next(self._statuses, SessionStatus.FINISHED)


class _DurabilityRedis:
    def __init__(self, *, statuses, pending_message=None, new_message=None):
        self.statuses = statuses
        self.pending_message = pending_message
        self.new_message = new_message
        self.pending_delivered = False
        self.new_delivered = False
        self.read_cursors = []
        self.acked = []
        self.delivered_count = 0

    async def xgroup_create(self, *args, **kwargs):
        return True

    async def xreadgroup(self, group, consumer, streams, **kwargs):
        cursor = next(iter(streams.values()))
        self.read_cursors.append(cursor)
        stream = b"audio:stream:session-1"

        if cursor != ">" and self.pending_message and not self.pending_delivered:
            self.pending_delivered = True
            delivered = (
                self.pending_message
                if isinstance(self.pending_message, list)
                else [self.pending_message]
            )
            self.delivered_count += len(delivered)
            return [(stream, delivered)]
        if cursor == ">" and self.new_message and not self.new_delivered:
            self.new_delivered = True
            delivered = (
                self.new_message
                if isinstance(self.new_message, list)
                else [self.new_message]
            )
            self.delivered_count += len(delivered)
            return [(stream, delivered)]
        # redis-py/Redis returns a truthy stream envelope with an empty entry list
        # for a pending cursor that has nothing owned by this consumer.
        return [(stream, [])]

    async def xack(self, stream, group, *message_ids):
        self.acked.extend(message_ids)
        return len(message_ids)

    async def execute_command(self, *args):
        pending = max(0, self.delivered_count - len(self.acked))
        return [
            [
                b"name",
                b"audio_persistence",
                b"pending",
                pending,
                b"lag",
                0,
            ]
        ]

    async def delete(self, *keys):
        return len(keys)


class _PersistedConversation:
    source_session_id = "session-1"
    audio_chunks_count = 0
    audio_total_duration = 0.0
    audio_compression_ratio = None

    async def save(self):
        return None


def _audio_message(message_id=b"1-0"):
    return (
        message_id,
        {
            b"audio_data": b"\x00\x01" * 160000,
            b"chunk_id": b"00001",
            b"conversation_id": b"conversation-1",
        },
    )


def _patch_persistence_dependencies(monkeypatch, audio_chunk_model):
    class FakeConversation:
        conversation_id = _QueryField()
        find_one = AsyncMock(return_value=_PersistedConversation())

    monkeypatch.setattr(audio_jobs, "SessionStore", _DurabilityStore)
    monkeypatch.setattr(audio_jobs, "AudioChunkDocument", audio_chunk_model)
    monkeypatch.setattr(audio_jobs, "Conversation", FakeConversation)
    monkeypatch.setattr(audio_jobs, "get_current_job", lambda: object())
    monkeypatch.setattr(audio_jobs, "check_job_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(
        audio_jobs, "get_resume_position", AsyncMock(return_value=(0, 0.0))
    )
    monkeypatch.setattr(
        audio_jobs, "encode_pcm_to_opus", AsyncMock(return_value=b"opus")
    )


@pytest.mark.asyncio
async def test_mongo_failure_never_acks_redis_audio(monkeypatch):
    class FailingAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        find_one = AsyncMock(return_value=None)

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            raise RuntimeError("mongo unavailable")

    _patch_persistence_dependencies(monkeypatch, FailingAudioChunk)
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        new_message=_audio_message(),
    )

    with pytest.raises(RuntimeError, match="mongo unavailable"):
        await audio_jobs.audio_streaming_persistence_job.__wrapped__(
            "session-1",
            "user-1",
            "client-1",
            redis_client=redis,
        )

    assert redis.acked == [], "audio must stay pending until Mongo commits it"


@pytest.mark.asyncio
async def test_restarted_worker_replays_pending_audio_before_new_entries(monkeypatch):
    inserted = AsyncMock()

    class SuccessfulAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        find_one = AsyncMock(return_value=None)

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            await inserted()

    _patch_persistence_dependencies(monkeypatch, SuccessfulAudioChunk)
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        pending_message=_audio_message(b"pending-1"),
    )

    await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=redis,
    )

    assert redis.read_cursors[0] != ">", "pending entries must be replayed first"
    inserted.assert_awaited_once()
    assert redis.acked == [b"pending-1"]


@pytest.mark.asyncio
async def test_wal_owner_rotation_never_merges_conversations(monkeypatch):
    inserted = []

    class OwnedAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        find_one = AsyncMock(return_value=None)

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            inserted.append(self)

    _patch_persistence_dependencies(monkeypatch, OwnedAudioChunk)
    first = _audio_message(b"1-0")
    second_id, second_fields = _audio_message(b"2-0")
    second_fields[b"conversation_id"] = b"conversation-2"
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        new_message=[first, (second_id, second_fields)],
    )

    await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=redis,
    )

    assert [chunk.conversation_id for chunk in inserted] == [
        "conversation-1",
        "conversation-2",
    ]
    assert [chunk.source_message_ids for chunk in inserted] == [["1-0"], ["2-0"]]
    assert redis.acked == [b"1-0", b"2-0"]


class _ProducerPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.pending_xadd = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def watch(self, *keys):
        return None

    async def get(self, key):
        return self.redis.owner

    async def hget(self, key, field):
        return self.redis.status

    async def unwatch(self):
        return None

    def multi(self):
        return None

    def xadd(self, stream, fields):
        self.pending_xadd = (stream, fields)

    async def execute(self):
        stream, fields = self.pending_xadd
        return [await self.redis.xadd(stream, fields)]


class _ProducerRedis:
    def __init__(self, error=None, owner=b"conversation-1", status=b"active"):
        self.error = error
        self.owner = owner
        self.status = status
        self.calls = []

    def pipeline(self, transaction=True):
        return _ProducerPipeline(self)

    async def xadd(self, stream, fields, **kwargs):
        self.calls.append((stream, fields, kwargs))
        if self.error:
            raise self.error
        return b"1-0"


@pytest.mark.asyncio
async def test_xadd_failure_keeps_audio_in_producer_buffer():
    redis = _ProducerRedis(RuntimeError("redis unavailable"))
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )
    audio = b"\x00\x01" * 4000  # exactly one 250 ms producer chunk

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await producer.add_audio_chunk(
            audio,
            "session-1",
            "user-1",
            "client-1",
            sample_rate=16000,
            channels=1,
            sample_width=2,
        )

    buffer = producer.session_buffers["session-1"]
    assert buffer.buffer == audio
    assert buffer.chunk_count == 0


@pytest.mark.asyncio
async def test_audio_stream_publish_does_not_trim_unpersisted_entries():
    redis = _ProducerRedis()
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )

    await producer.add_audio_chunk(
        b"\x00\x01" * 4000,
        "session-1",
        "user-1",
        "client-1",
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )

    assert "maxlen" not in redis.calls[0][2]
    assert redis.calls[0][1][b"conversation_id"] == b"conversation-1"


@pytest.mark.asyncio
async def test_ownerless_session_rejects_xadd_without_discarding_audio():
    redis = _ProducerRedis(owner=None)
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )
    audio = b"\x00\x01" * 4000

    with pytest.raises(RuntimeError, match="has no conversation owner"):
        await producer.add_audio_chunk(
            audio,
            "session-1",
            "user-1",
            "client-1",
            sample_rate=16000,
            channels=1,
            sample_width=2,
        )

    assert producer.session_buffers["session-1"].buffer == audio
    assert redis.calls == []


@pytest.mark.asyncio
async def test_terminal_session_rejects_late_audio_without_xadd():
    redis = _ProducerRedis(status=b"finished")
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )
    audio = b"\x00\x01" * 4000

    with pytest.raises(RuntimeError, match="is not active"):
        await producer.add_audio_chunk(
            audio,
            "session-1",
            "user-1",
            "client-1",
            sample_rate=16000,
            channels=1,
            sample_width=2,
        )

    assert producer.session_buffers["session-1"].buffer == audio
    assert redis.calls == []


@pytest.mark.asyncio
async def test_batch_transcription_refuses_to_invent_missing_durable_owner(monkeypatch):
    class MissingOwnerConversation:
        source_session_id = _QueryField()
        always_persist = _QueryField()
        processing_status = _QueryField()
        ConversationStatus = transcription_jobs.Conversation.ConversationStatus
        find_one = AsyncMock(return_value=None)

    monkeypatch.setattr(transcription_jobs, "Conversation", MissingOwnerConversation)

    with pytest.raises(RuntimeError, match="refusing to invent"):
        await transcription_jobs.create_audio_only_conversation(
            "session-1", "user-1", "client-1"
        )


@pytest.mark.asyncio
async def test_batch_transcription_reuses_session_durable_owner(monkeypatch):
    placeholder = SimpleNamespace(
        conversation_id="conversation-1",
        processing_status="failed",
        title="old",
        summary="old",
        save=AsyncMock(),
    )

    class DurableOwnerConversation:
        source_session_id = _QueryField()
        always_persist = _QueryField()
        processing_status = _QueryField()
        ConversationStatus = transcription_jobs.Conversation.ConversationStatus
        find_one = AsyncMock(return_value=placeholder)

    monkeypatch.setattr(transcription_jobs, "Conversation", DurableOwnerConversation)

    resolved = await transcription_jobs.create_audio_only_conversation(
        "session-1", "user-1", "client-1"
    )

    assert resolved is placeholder
    assert placeholder.processing_status == "active"
    placeholder.save.assert_awaited_once()


class _LaggingStreamRedis:
    def __init__(self):
        self.deleted = []

    async def exists(self, stream_name):
        return True

    async def hget(self, key, field):
        return b"finished"

    async def execute_command(self, *args):
        return [
            [
                b"name",
                b"streaming-transcription",
                b"pending",
                0,
                b"lag",
                0,
            ],
            [b"name", b"audio_persistence", b"pending", 0, b"lag", 4],
        ]

    async def delete(self, stream_name):
        self.deleted.append(stream_name)


@pytest.mark.asyncio
async def test_stream_with_unconsumed_persistence_lag_is_not_deleted():
    redis = _LaggingStreamRedis()
    consumer = object.__new__(StreamingTranscriptionConsumer)
    consumer.redis_client = redis
    consumer.group_name = "streaming-transcription"

    await consumer._try_delete_finished_stream("audio:stream:client-1")

    assert redis.deleted == []


def test_persistence_runtime_state_has_only_explicit_forward_transitions():
    state = PersistenceRuntimeState()
    assert state.session is SessionPhase.ACTIVE
    assert state.reader is ReadPhase.RECOVERING_PENDING
    assert state.outcome is PersistenceOutcome.RUNNING

    with pytest.raises(RuntimeError, match="while the session is active"):
        state.complete()

    state.pending_recovered()
    state.begin_draining()
    state.complete()
    assert state.outcome is PersistenceOutcome.COMPLETE

    with pytest.raises(RuntimeError, match="already complete"):
        state.fail()


@pytest.mark.asyncio
async def test_replay_after_mongo_commit_before_xack_is_idempotent(monkeypatch):
    source_ids = ["pending-1"]
    existing = SimpleNamespace(
        conversation_id="conversation-1",
        chunk_index=0,
        original_size=320000,
        compressed_size=4,
        end_time=10.0,
        source_message_ids=source_ids,
    )
    inserted = AsyncMock()

    class IdempotentAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        find_one = AsyncMock(return_value=existing)

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            await inserted()

    _patch_persistence_dependencies(monkeypatch, IdempotentAudioChunk)
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        pending_message=_audio_message(b"pending-1"),
    )

    await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=redis,
    )

    inserted.assert_not_awaited()
    assert redis.acked == [b"pending-1"]


@pytest.mark.asyncio
async def test_finalization_flushes_residual_audio_before_terminal_state():
    redis = _ProducerRedis()
    producer = AudioStreamProducer(redis)
    producer.store = SimpleNamespace(
        get_audio_format=AsyncMock(return_value=(16000, 1, 2)),
        mark_finalizing=AsyncMock(),
    )
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
        buffer=b"residual-pcm",
    )

    await producer.finalize_session("session-1", "websocket_disconnect")

    assert redis.calls[0][1][b"audio_data"] == b"residual-pcm"
    assert redis.calls[1][1][b"chunk_id"] == b"END"
    producer.store.mark_finalizing.assert_awaited_once()
    assert "session-1" not in producer.session_buffers


@pytest.mark.asyncio
async def test_finalization_append_error_does_not_advance_or_discard_buffer():
    redis = _ProducerRedis(RuntimeError("redis unavailable"))
    producer = AudioStreamProducer(redis)
    producer.store = SimpleNamespace(
        get_audio_format=AsyncMock(return_value=(16000, 1, 2)),
        mark_finalizing=AsyncMock(),
    )
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
        buffer=b"residual-pcm",
    )

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await producer.finalize_session("session-1", "websocket_disconnect")

    producer.store.mark_finalizing.assert_not_awaited()
    assert producer.session_buffers["session-1"].buffer == b"residual-pcm"
