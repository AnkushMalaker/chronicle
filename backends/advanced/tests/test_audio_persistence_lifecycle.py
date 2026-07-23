"""Lifecycle invariants for the session-scoped audio persistence worker."""

import time
from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.services.audio_stream.session_store import SessionStatus
from advanced_omi_backend.workers import audio_jobs

pytestmark = pytest.mark.unit


class _RaceRedis:
    """Minimal Redis adapter for the close/finalize race seen in production."""

    def __init__(self):
        # Startup sees the real conversation, the first loop keeps it, then the
        # conversation worker clears the pointer just before session finalization.
        self._conversation_reads = iter(
            (b"real-conversation", b"real-conversation", None)
        )
        self._stream_reads = iter(
            (
                [
                    (
                        b"audio:stream:session-1",
                        [
                            (
                                b"1-0",
                                {
                                    b"audio_data": b"\x00\x01" * 160,
                                    b"chunk_id": b"1",
                                    b"conversation_id": b"real-conversation",
                                },
                            )
                        ],
                    )
                ],
                [],
            )
        )
        self.set_calls = []

    async def xgroup_create(self, *args, **kwargs):
        return True

    async def get(self, key):
        if key == "conversation:current:session-1":
            return next(self._conversation_reads)
        return None

    async def set(self, key, value, **kwargs):
        self.set_calls.append((key, value, kwargs))
        return True

    async def delete(self, *keys):
        return len(keys)

    async def xreadgroup(self, *args, **kwargs):
        return next(self._stream_reads, [])

    async def xack(self, *args, **kwargs):
        return 1

    async def execute_command(self, *args):
        return [
            [
                b"name",
                b"audio_persistence",
                b"pending",
                0,
                b"lag",
                0,
            ]
        ]


class _RaceSessionStore:
    def __init__(self, redis_client):
        self._statuses = iter(
            (SessionStatus.ACTIVE, SessionStatus.ACTIVE, SessionStatus.FINALIZING)
        )
        self._conversation_reads = iter(
            ("real-conversation", "real-conversation", None)
        )

    async def get_audio_format(self, session_id):
        return 16000, 1, 2

    async def get_last_chunk_at(self, session_id):
        return time.time()

    async def is_websocket_connected(self, session_id):
        return True

    async def get_status(self, session_id):
        return next(self._statuses, SessionStatus.FINALIZING)

    async def get_current_conversation_id(self, session_id):
        raise AssertionError("persistence must use the immutable WAL owner")


class _PersistedConversation:
    source_session_id = "session-1"
    audio_chunks_count = 0
    audio_total_duration = 0.0
    audio_compression_ratio = None

    async def save(self):
        return None


class _QueryField:
    def __eq__(self, value):
        return value


@pytest.mark.asyncio
async def test_pointer_clear_immediately_before_finalization_does_not_create_phantom(
    monkeypatch,
):
    """A closing conversation must not be replaced before a terminal session state lands.

    This is the production sequence that created ``dcadedf5``: the conversation
    worker deleted ``conversation:current``; persistence observed the session as
    active for one last iteration and eagerly inserted a new placeholder; the
    finalizing state arrived on the next iteration, leaving that placeholder active
    forever with zero audio.
    """

    redis = _RaceRedis()
    inserted_conversations = AsyncMock()

    class FakeConversation:
        conversation_id = _QueryField()
        ConversationStatus = audio_jobs.Conversation.ConversationStatus
        find_one = AsyncMock(return_value=_PersistedConversation())

        def __init__(self, **kwargs):
            self.conversation_id = "phantom-conversation"

        async def insert(self):
            await inserted_conversations()

    class FakeAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        find_one = AsyncMock(return_value=None)

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            return None

    monkeypatch.setattr(audio_jobs, "SessionStore", _RaceSessionStore)
    monkeypatch.setattr(audio_jobs, "Conversation", FakeConversation)
    monkeypatch.setattr(audio_jobs, "AudioChunkDocument", FakeAudioChunk)
    monkeypatch.setattr(audio_jobs, "get_current_job", lambda: object())
    monkeypatch.setattr(audio_jobs, "check_job_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(
        audio_jobs, "get_resume_position", AsyncMock(return_value=(0, 0.0))
    )
    monkeypatch.setattr(
        audio_jobs, "encode_pcm_to_opus", AsyncMock(return_value=b"opus")
    )

    result = await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=redis,
    )

    assert result["total_mongo_chunks"] == 1
    inserted_conversations.assert_not_awaited()
    assert redis.set_calls == []
