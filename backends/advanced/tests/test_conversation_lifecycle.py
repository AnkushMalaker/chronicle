"""Tests for speech-driven Conversation materialization and teardown."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.services.audio_stream import conversation_lifecycle
from advanced_omi_backend.services.audio_stream.session_store import SessionStatus
from advanced_omi_backend.workers import conversation_jobs

pytestmark = pytest.mark.unit


class _QueryField:
    def __eq__(self, value):
        return value


class _CaptureModel:
    capture_session_id = _QueryField()
    result = None

    @classmethod
    async def find_one(cls, _query):
        return cls.result


class _ConversationModel:
    segmentation_key = _QueryField()
    result = None

    @classmethod
    async def find_one(cls, _query):
        return cls.result


@pytest.fixture(autouse=True)
def _reset_models(monkeypatch):
    _CaptureModel.result = None
    _ConversationModel.result = None
    monkeypatch.setattr(conversation_lifecycle, "AudioCaptureSession", _CaptureModel)
    monkeypatch.setattr(conversation_lifecycle, "Conversation", _ConversationModel)


@pytest.mark.asyncio
async def test_detected_materialization_requires_a_capture_session():
    with pytest.raises(RuntimeError, match="does not exist"):
        await conversation_lifecycle.materialize_detected_conversation(
            capture_session_id="capture-1",
            user_id="user-1",
            client_id="client-1",
            speech_detected_at=1_786_528_800.0,
        )


@pytest.mark.asyncio
async def test_detected_materialization_rejects_capture_identity_mismatch():
    _CaptureModel.result = SimpleNamespace(
        user_id="other-user",
        client_id="client-1",
        started_at=datetime(2026, 8, 12, 10, tzinfo=timezone.utc),
    )

    with pytest.raises(RuntimeError, match="does not match"):
        await conversation_lifecycle.materialize_detected_conversation(
            capture_session_id="capture-1",
            user_id="user-1",
            client_id="client-1",
            speech_detected_at=1_786_528_800.0,
        )


@pytest.mark.asyncio
async def test_detected_materialization_creates_only_after_speech(monkeypatch):
    captured_at = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    _CaptureModel.result = SimpleNamespace(
        user_id="user-1", client_id="client-1", started_at=captured_at
    )
    created = SimpleNamespace(conversation_id="conversation-1", insert=AsyncMock())
    factory = Mock(return_value=created)
    monkeypatch.setattr(conversation_lifecycle, "create_conversation", factory)

    result = await conversation_lifecycle.materialize_detected_conversation(
        capture_session_id="capture-1",
        user_id="user-1",
        client_id="client-1",
        speech_detected_at=captured_at.timestamp() + 20,
        pre_roll_seconds=5,
    )

    assert result.created is True
    assert result.conversation is created
    created.insert.assert_awaited_once()
    kwargs = factory.call_args.kwargs
    assert kwargs["origin"] == "detected"
    assert kwargs["started_at"] == captured_at.replace(second=15)
    assert kwargs["segmentation_key"].startswith("detected:capture-1:")


@pytest.mark.asyncio
async def test_detected_materialization_is_idempotent(monkeypatch):
    captured_at = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    _CaptureModel.result = SimpleNamespace(
        user_id="user-1", client_id="client-1", started_at=captured_at
    )
    existing = SimpleNamespace(conversation_id="conversation-existing")
    _ConversationModel.result = existing
    factory = Mock()
    monkeypatch.setattr(conversation_lifecycle, "create_conversation", factory)

    result = await conversation_lifecycle.materialize_detected_conversation(
        capture_session_id="capture-1",
        user_id="user-1",
        client_id="client-1",
        speech_detected_at=captured_at.timestamp() + 20,
    )

    assert result.created is False
    assert result.conversation is existing
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_insert_race_returns_segmentation_winner(monkeypatch):
    captured_at = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    _CaptureModel.result = SimpleNamespace(
        user_id="user-1", client_id="client-1", started_at=captured_at
    )
    candidate = SimpleNamespace(
        conversation_id="candidate",
        insert=AsyncMock(side_effect=DuplicateKeyError("duplicate")),
    )
    winner = SimpleNamespace(conversation_id="winner")
    calls = 0

    async def find_one(_query):
        nonlocal calls
        calls += 1
        return None if calls == 1 else winner

    monkeypatch.setattr(_ConversationModel, "find_one", find_one)
    monkeypatch.setattr(
        conversation_lifecycle, "create_conversation", Mock(return_value=candidate)
    )

    result = await conversation_lifecycle.materialize_detected_conversation(
        capture_session_id="capture-1",
        user_id="user-1",
        client_id="client-1",
        speech_detected_at=captured_at.timestamp() + 20,
    )

    assert result.created is False
    assert result.conversation is winner


class _EndRedis:
    def __init__(self):
        self.deleted = []

    async def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


class _EndStore:
    status = SessionStatus.ACTIVE
    last_instance = None

    def __init__(self, _redis_client):
        self.cleared = []
        self.expired = False
        self.persisted = False
        type(self).last_instance = self

    async def clear_active_conversation(self, session_id, *, expected_id=None):
        self.cleared.append((session_id, expected_id))
        return True

    async def increment_conversation_count(self, _session_id):
        return 1

    async def get_status_ws_reason(self, _session_id):
        return self.status, self.status == SessionStatus.ACTIVE, ""

    async def persist_session(self, _session_id):
        self.persisted = True

    async def expire_session(self, _session_id, _ttl):
        self.expired = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expects_restart"),
    ((SessionStatus.ACTIVE, True), (SessionStatus.FINALIZING, False)),
)
async def test_conversation_end_clears_semantic_pointer_and_rearms_only_active_session(
    monkeypatch, status, expects_restart
):
    class FakeConversationModel:
        conversation_id = _QueryField()
        EndReason = conversation_jobs.Conversation.EndReason

        @classmethod
        async def find_one(cls, _query):
            return SimpleNamespace(end_reason=None, completed_at=None, save=AsyncMock())

    _EndStore.status = status
    enqueue_speech = Mock()
    monkeypatch.setattr(conversation_jobs, "SessionStore", _EndStore)
    monkeypatch.setattr(conversation_jobs, "Conversation", FakeConversationModel)
    monkeypatch.setattr(conversation_jobs, "enqueue_speech_detection", enqueue_speech)
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", lambda *args: None)

    await conversation_jobs.handle_end_of_conversation(
        session_id="session-1",
        conversation_id="conversation-1",
        client_id="client-1",
        user_id="user-1",
        start_time=0.0,
        last_result_count=1,
        timeout_triggered=True,
        redis_client=_EndRedis(),
        end_reason="inactivity_timeout",
    )

    store = _EndStore.last_instance
    assert store.cleared == [("session-1", "conversation-1")]
    assert enqueue_speech.call_count == int(expects_restart)
