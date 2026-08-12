"""Streaming audio must never outlive its durable-persistence consumer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.client import ClientState
from advanced_omi_backend.controllers import websocket_controller

pytestmark = pytest.mark.unit


async def _ignore_sse(*args, **kwargs):
    return None


class FailingFinalizeProducer:
    async def finalize_session(self, *args, **kwargs):
        raise RuntimeError("flush failed")


@pytest.mark.asyncio
async def test_finalize_failure_retains_session_for_explicit_retry():
    state = ClientState("client-1", "user-1")
    state.stream_session_id = "session-1"
    state.last_persistence_healthcheck = 123.0

    with pytest.raises(RuntimeError, match="flush failed"):
        await websocket_controller._finalize_streaming_session(
            state,
            FailingFinalizeProducer(),
            "user-1",
            "user@example.com",
            "client-1",
        )

    assert state.stream_session_id == "session-1"
    assert state.last_persistence_healthcheck == 123.0


@pytest.mark.asyncio
async def test_disconnect_finalize_error_does_not_remove_client_state(monkeypatch):
    state = ClientState("client-1", "user-1")
    state.stream_session_id = "session-1"
    remove_client = AsyncMock()
    manager = SimpleNamespace(
        get_client=lambda client_id: state,
        remove_client_with_cleanup=remove_client,
    )
    redis = SimpleNamespace(aclose=AsyncMock())
    view = SimpleNamespace(
        session_id="session-1",
        client_id="client-1",
        user_id="user-1",
        status=websocket_controller.SessionStatus.ACTIVE,
        stream_name="audio:stream:session-1",
    )

    class Store:
        def __init__(self, redis_client):
            pass

        async def read(self, session_id):
            return view

    monkeypatch.setattr(websocket_controller, "get_client_manager", lambda: manager)
    monkeypatch.setattr(
        websocket_controller, "create_async_redis", lambda **kwargs: redis
    )
    monkeypatch.setattr(websocket_controller, "SessionStore", Store)
    monkeypatch.setattr(
        websocket_controller,
        "get_audio_stream_producer",
        lambda: FailingFinalizeProducer(),
    )

    with pytest.raises(RuntimeError, match="flush failed"):
        await websocket_controller.cleanup_client_state("client-1")

    remove_client.assert_not_awaited()
    assert state.stream_session_id == "session-1"
    redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_transitions_after_wal_terminal_commit(monkeypatch):
    transitions = []

    class Store:
        async def mark_complete(self, session_id, reason):
            transitions.append(("producer_finished", session_id, reason))

    class Producer:
        store = Store()

        async def finalize_session(self, session_id, completion_reason):
            transitions.append(("wal_terminal", session_id, completion_reason))

    state = ClientState("client-1", "user-1")
    state.stream_session_id = "session-1"
    monkeypatch.setattr(websocket_controller, "publish_sse_event_async", _ignore_sse)

    await websocket_controller._finalize_streaming_session(
        state,
        Producer(),
        "user-1",
        "user@example.com",
        "client-1",
    )

    assert transitions == [
        ("wal_terminal", "session-1", "user_stopped"),
        ("producer_finished", "session-1", "user_stopped"),
    ]
    assert state.stream_session_id is None


@pytest.mark.asyncio
async def test_active_stream_periodically_ensures_persistence(monkeypatch):
    state = ClientState("client-1", "user-1")
    state.stream_session_id = "session-1"
    state.last_persistence_healthcheck = 0.0
    producer = AsyncMock()
    ensured = []
    monkeypatch.setattr(
        websocket_controller,
        "ensure_audio_persistence",
        lambda session_id, user_id, client_id: ensured.append(
            (session_id, user_id, client_id)
        )
        or "audio-persist_session-1",
    )
    monkeypatch.setattr(websocket_controller.time, "monotonic", lambda: 10.0)

    await websocket_controller._handle_streaming_mode_audio(
        state,
        producer,
        b"audio",
        {"rate": 16000, "channels": 1, "width": 2},
        "user-1",
        "user@example.com",
        "client-1",
    )

    assert ensured == [("session-1", "user-1", "client-1")]
    producer.add_audio_chunk.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_fails_closed_before_publish_when_persistence_is_unavailable(
    monkeypatch,
):
    state = ClientState("client-1", "user-1")
    state.stream_session_id = "session-1"
    producer = AsyncMock()
    monkeypatch.setattr(websocket_controller.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        websocket_controller,
        "ensure_audio_persistence",
        lambda *args: (_ for _ in ()).throw(RuntimeError("persistence unavailable")),
    )

    with pytest.raises(RuntimeError, match="persistence unavailable"):
        await websocket_controller._handle_streaming_mode_audio(
            state,
            producer,
            b"audio",
            {"rate": 16000, "channels": 1, "width": 2},
            "user-1",
            "user@example.com",
            "client-1",
        )

    producer.add_audio_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_assigns_durable_owner_before_enqueuing_workers(monkeypatch):
    state = ClientState("client-1", "user-1")
    state.client_id = "client-1"
    producer = AsyncMock()
    transitions = []

    async def assign_owner(*args, **kwargs):
        transitions.append("owner_assigned")
        return SimpleNamespace(conversation_id="conversation-1")

    def start_jobs(**kwargs):
        transitions.append("workers_live")
        return {"speech_detection": "speech-1", "audio_persistence": "persist-1"}

    model = SimpleNamespace(model_provider="deepgram", name="stt")
    registry = SimpleNamespace(get_default=lambda model_type: model)
    monkeypatch.setattr(websocket_controller, "get_models_registry", lambda: registry)
    monkeypatch.setattr(
        websocket_controller, "ensure_active_session_placeholder", assign_owner
    )
    monkeypatch.setattr(websocket_controller, "start_streaming_jobs", start_jobs)
    monkeypatch.setattr(websocket_controller, "publish_sse_event_async", _ignore_sse)
    monkeypatch.setattr(
        websocket_controller.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="capture1"),
    )

    await websocket_controller._initialize_streaming_session(
        state,
        producer,
        "user-1",
        "user@example.com",
        "client-1",
        {"rate": 16000, "channels": 1, "width": 2},
    )

    assert transitions == ["owner_assigned", "workers_live"]
    producer.update_session_job_ids.assert_awaited_once_with(
        session_id="client-1-capture1",
        speech_detection_job_id="speech-1",
        audio_persistence_job_id="persist-1",
    )


@pytest.mark.asyncio
async def test_connect_rejects_ingress_when_owner_assignment_fails(monkeypatch):
    state = ClientState("client-1", "user-1")
    state.client_id = "client-1"
    producer = AsyncMock()
    started = AsyncMock()
    model = SimpleNamespace(model_provider="deepgram", name="stt")
    registry = SimpleNamespace(get_default=lambda model_type: model)
    monkeypatch.setattr(websocket_controller, "get_models_registry", lambda: registry)
    monkeypatch.setattr(
        websocket_controller,
        "ensure_active_session_placeholder",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(websocket_controller, "start_streaming_jobs", started)

    with pytest.raises(RuntimeError, match="durable audio owner"):
        await websocket_controller._initialize_streaming_session(
            state,
            producer,
            "user-1",
            "user@example.com",
            "client-1",
            {"rate": 16000, "channels": 1, "width": 2},
        )

    started.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_uses_a_distinct_capture_session_and_wal(monkeypatch):
    ids = iter(("capture1", "capture2"))
    monkeypatch.setattr(
        websocket_controller.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(ids)),
    )
    model = SimpleNamespace(model_provider="deepgram", name="stt")
    registry = SimpleNamespace(get_default=lambda model_type: model)
    monkeypatch.setattr(websocket_controller, "get_models_registry", lambda: registry)
    monkeypatch.setattr(
        websocket_controller,
        "ensure_active_session_placeholder",
        AsyncMock(return_value=SimpleNamespace(conversation_id="conversation-1")),
    )
    monkeypatch.setattr(
        websocket_controller,
        "start_streaming_jobs",
        lambda **kwargs: {
            "speech_detection": "speech-1",
            "audio_persistence": "persist-1",
        },
    )
    monkeypatch.setattr(websocket_controller, "publish_sse_event_async", _ignore_sse)

    first = ClientState("client-1", "user-1")
    second = ClientState("client-1", "user-1")
    first_producer = AsyncMock()
    second_producer = AsyncMock()

    await websocket_controller._initialize_streaming_session(
        first,
        first_producer,
        "user-1",
        "user@example.com",
        "client-1",
        {"rate": 16000, "channels": 1, "width": 2},
    )
    await websocket_controller._initialize_streaming_session(
        second,
        second_producer,
        "user-1",
        "user@example.com",
        "client-1",
        {"rate": 16000, "channels": 1, "width": 2},
    )

    assert first.stream_session_id == "client-1-capture1"
    assert second.stream_session_id == "client-1-capture2"
    assert first.stream_session_id != second.stream_session_id
