"""Lifecycle invariants for the session-scoped audio persistence worker."""

from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.services.audio_stream.session_store import SessionStatus
from advanced_omi_backend.workers import audio_jobs

pytestmark = pytest.mark.unit


class _RaceRedis:
    """Minimal Redis adapter for the close/finalize race seen in production."""

    def __init__(self):
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
                                    b"capture_session_id": b"session-1",
                                    b"captured_at": b"1770000000.0",
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

    async def get_audio_format(self, session_id):
        return 16000, 1, 2

    async def get_status(self, session_id):
        return next(self._statuses, SessionStatus.FINALIZING)


class _PersistedCapture:
    capture_session_id = "session-1"
    user_id = "user-1"
    client_id = "client-1"
    status = "active"
    ended_at = None

    async def save(self):
        return None


class _QueryField:
    def __eq__(self, value):
        return value


class _EmptyFind:
    def sort(self, *_args, **_kwargs):
        return self

    async def first_or_none(self):
        return None


@pytest.mark.asyncio
async def test_pointer_clear_immediately_before_finalization_does_not_create_phantom(
    monkeypatch,
):
    """Capture finalization never reads or creates a semantic Conversation."""

    redis = _RaceRedis()

    class FakeCapture:
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=_PersistedCapture())

    class FakeAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=None)
        find = staticmethod(lambda *_args, **_kwargs: _EmptyFind())

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            return None

    monkeypatch.setattr(audio_jobs, "SessionStore", _RaceSessionStore)
    monkeypatch.setattr(audio_jobs, "AudioCaptureSession", FakeCapture)
    monkeypatch.setattr(audio_jobs, "AudioChunkDocument", FakeAudioChunk)
    monkeypatch.setattr(audio_jobs, "get_current_job", lambda: object())
    monkeypatch.setattr(audio_jobs, "check_job_alive", AsyncMock(return_value=True))
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
    assert redis.set_calls == []
