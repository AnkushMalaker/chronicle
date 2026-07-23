"""Tests for the streaming-session conversation-assignment module."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from advanced_omi_backend.services.audio_stream import conversation_lifecycle
from advanced_omi_backend.services.audio_stream.session_store import SessionStatus
from advanced_omi_backend.workers import conversation_jobs

pytestmark = pytest.mark.unit


class _QueryField:
    def __eq__(self, value):
        return value


class _Store:
    def __init__(self, statuses, current_id=None):
        self._statuses = iter(statuses)
        self.current_id = current_id
        self.assignments = []
        self.clears = []

    @asynccontextmanager
    async def conversation_create_lock(self, session_id):
        yield True

    async def get_status(self, session_id):
        return next(self._statuses, SessionStatus.ACTIVE)

    async def get_current_conversation_id(self, session_id):
        return self.current_id

    async def clear_current_conversation(self, session_id, *, expected_id=None):
        self.clears.append((session_id, expected_id))
        if expected_id is None or self.current_id == expected_id:
            self.current_id = None
            return True
        return False

    async def set_current_conversation(self, session_id, conversation_id, *, ttl=86400):
        self.current_id = conversation_id
        self.assignments.append((session_id, conversation_id, ttl))

    async def assign_current_conversation_if_active(
        self, session_id, conversation_id, *, ttl=86400
    ):
        if (
            await self.get_status(session_id) != SessionStatus.ACTIVE
            or self.current_id is not None
        ):
            return False
        await self.set_current_conversation(session_id, conversation_id, ttl=ttl)
        return True

    async def replace_current_conversation_if_active(
        self, session_id, expected_id, replacement_id, *, ttl=86400
    ):
        if (
            await self.get_status(session_id) != SessionStatus.ACTIVE
            or self.current_id != expected_id
        ):
            return False
        await self.set_current_conversation(session_id, replacement_id, ttl=ttl)
        return True


def _conversation_model(existing=None):
    class FakeConversation:
        conversation_id = _QueryField()
        ConversationStatus = conversation_lifecycle.Conversation.ConversationStatus
        find_one_result = existing
        inserted = []
        deleted_candidates = []

        def __init__(self, **kwargs):
            self.conversation_id = "placeholder-1"
            self.processing_status = kwargs["processing_status"]
            self.deleted = False

        @classmethod
        async def find_one(cls, query):
            return cls.find_one_result

        async def insert(self):
            type(self).inserted.append(self.conversation_id)

        async def delete(self):
            type(self).deleted_candidates.append(self.conversation_id)

    return FakeConversation


@pytest.mark.asyncio
async def test_terminal_session_never_gets_a_placeholder(monkeypatch):
    model = _conversation_model()
    monkeypatch.setattr(conversation_lifecycle, "Conversation", model)
    store = _Store([SessionStatus.FINALIZING])

    assignment = await conversation_lifecycle.ensure_active_session_placeholder(
        store,
        session_id="session-1",
        user_id="user-1",
        client_id="client-1",
    )

    assert assignment is None
    assert model.inserted == []
    assert store.assignments == []


@pytest.mark.asyncio
async def test_active_session_creation_publishes_one_assignment(monkeypatch):
    model = _conversation_model()
    monkeypatch.setattr(conversation_lifecycle, "Conversation", model)
    store = _Store([SessionStatus.ACTIVE, SessionStatus.ACTIVE])

    assignment = await conversation_lifecycle.ensure_active_session_placeholder(
        store,
        session_id="session-1",
        user_id="user-1",
        client_id="client-1",
    )

    assert assignment.conversation_id == "placeholder-1"
    assert assignment.created is True
    assert model.inserted == ["placeholder-1"]
    assert store.assignments == [("session-1", "placeholder-1", None)]


@pytest.mark.asyncio
async def test_finalization_during_mongo_insert_rolls_back_candidate(monkeypatch):
    model = _conversation_model()
    monkeypatch.setattr(conversation_lifecycle, "Conversation", model)
    store = _Store([SessionStatus.ACTIVE, SessionStatus.FINALIZING])

    assignment = await conversation_lifecycle.ensure_active_session_placeholder(
        store,
        session_id="session-1",
        user_id="user-1",
        client_id="client-1",
    )

    assert assignment is None
    assert model.deleted_candidates == ["placeholder-1"]
    assert store.assignments == []


@pytest.mark.asyncio
async def test_finalization_after_lost_claim_does_not_return_competing_pointer(
    monkeypatch,
):
    existing = SimpleNamespace(
        processing_status="active",
        deleted=False,
        always_persist=True,
        has_meaningful_transcript=False,
    )
    model = _conversation_model(existing=existing)
    monkeypatch.setattr(conversation_lifecycle, "Conversation", model)

    class FinalizingStore(_Store):
        async def assign_current_conversation_if_active(
            self, session_id, conversation_id, *, ttl=86400
        ):
            self.current_id = "late-pointer"
            return False

    store = FinalizingStore(
        [SessionStatus.ACTIVE, SessionStatus.FINALIZING], current_id=None
    )

    assignment = await conversation_lifecycle.ensure_active_session_placeholder(
        store,
        session_id="session-1",
        user_id="user-1",
        client_id="client-1",
    )

    assert assignment is None
    assert model.deleted_candidates == ["placeholder-1"]


@pytest.mark.asyncio
async def test_active_rotation_swaps_owner_without_unassigned_gap(monkeypatch):
    model = _conversation_model()
    monkeypatch.setattr(conversation_lifecycle, "Conversation", model)
    store = _Store(
        [SessionStatus.ACTIVE, SessionStatus.ACTIVE],
        current_id="conversation-1",
    )

    assignment = await conversation_lifecycle.rotate_active_session_placeholder(
        store,
        session_id="session-1",
        expected_conversation_id="conversation-1",
        user_id="user-1",
        client_id="client-1",
    )

    assert assignment.conversation_id == "placeholder-1"
    assert assignment.created is True
    assert store.current_id == "placeholder-1"
    assert store.clears == []
    assert store.assignments == [("session-1", "placeholder-1", None)]


@pytest.mark.asyncio
async def test_terminal_rotation_does_not_create_successor(monkeypatch):
    model = _conversation_model()
    monkeypatch.setattr(conversation_lifecycle, "Conversation", model)
    store = _Store([SessionStatus.FINALIZING], current_id="conversation-1")

    assignment = await conversation_lifecycle.rotate_active_session_placeholder(
        store,
        session_id="session-1",
        expected_conversation_id="conversation-1",
        user_id="user-1",
        client_id="client-1",
    )

    assert assignment is None
    assert model.inserted == []
    assert store.current_id == "conversation-1"


class _EndRedis:
    def __init__(self):
        self.deleted = []

    async def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


class _EndStore:
    status = SessionStatus.ACTIVE

    def __init__(self, redis_client):
        self.expired = False
        self.persisted = False

    @asynccontextmanager
    async def conversation_create_lock(self, session_id):
        yield True

    async def clear_current_conversation(self, session_id, *, expected_id=None):
        return True

    async def increment_conversation_count(self, session_id):
        return 1

    async def get_status_ws_reason(self, session_id):
        return self.status, self.status == SessionStatus.ACTIVE, ""

    async def persist_session(self, session_id):
        self.persisted = True

    async def expire_session(self, session_id, ttl):
        self.expired = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expects_placeholder", "expects_restart"),
    (
        (SessionStatus.ACTIVE, True, True),
        (SessionStatus.FINALIZING, False, False),
    ),
)
async def test_conversation_end_assigns_successor_only_for_active_session(
    monkeypatch, status, expects_placeholder, expects_restart
):
    class FakeConversationModel:
        conversation_id = _QueryField()
        EndReason = conversation_jobs.Conversation.EndReason

        @classmethod
        async def find_one(cls, query):
            return SimpleNamespace(
                always_persist=True,
                end_reason=None,
                completed_at=None,
                save=AsyncMock(),
            )

    _EndStore.status = status
    ensure_placeholder = AsyncMock(
        return_value=SimpleNamespace(conversation_id="next-placeholder")
    )
    enqueue_speech = Mock()

    monkeypatch.setattr(conversation_jobs, "SessionStore", _EndStore)
    monkeypatch.setattr(conversation_jobs, "Conversation", FakeConversationModel)
    monkeypatch.setattr(
        conversation_jobs, "ensure_active_session_placeholder", ensure_placeholder
    )
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

    assert ensure_placeholder.await_count == int(expects_placeholder)
    assert enqueue_speech.call_count == int(expects_restart)
