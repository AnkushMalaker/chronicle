"""Regression coverage for conversations excluded from user memory."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.services.memory.agent.memory_agent import MemoryAgentResult
from backend.services.memory.providers.chronicle import MemoryService
from backend.workers import memory_jobs


@pytest.mark.asyncio
async def test_memory_worker_refuses_excluded_conversation(monkeypatch):
    conversation = SimpleNamespace(memory_excluded=True)
    find_one = AsyncMock(return_value=conversation)
    get_memory_service = Mock()
    conversation_model = SimpleNamespace(
        conversation_id=object(),
        find_one=find_one,
    )
    monkeypatch.setattr(memory_jobs, "Conversation", conversation_model)
    monkeypatch.setattr(memory_jobs, "get_memory_service", get_memory_service)

    undecorated_job = memory_jobs.process_memory_job.__wrapped__.__wrapped__
    result = await undecorated_job("excluded-conversation", redis_client=None)

    assert result == {
        "success": True,
        "skipped": True,
        "reason": "memory_excluded",
        "conversation_id": "excluded-conversation",
    }
    get_memory_service.assert_not_called()


@pytest.mark.asyncio
async def test_memory_worker_surfaces_deterministic_note_fallback_as_system_warning(
    tmp_path, monkeypatch
):
    conversation_id = "conversation-worker-fallback"
    conversation = SimpleNamespace(
        memory_excluded=False,
        client_id="test-client",
        user_id="test-user",
        segments=[
            SimpleNamespace(
                start=0,
                end=60,
                text="Preserve this source when the memory agent truncates.",
                speaker="alex",
                segment_type="speech",
            )
        ],
        transcript="alex: Preserve this source when the memory agent truncates.",
        created_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        audio_total_duration=60,
        title="Worker fallback",
    )
    conversation_model = SimpleNamespace(
        conversation_id=object(),
        find_one=AsyncMock(return_value=conversation),
    )
    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=False,
        )
    )
    system_events = []

    class TruncatedAgent:
        def __init__(self, _root):
            pass

        async def run(self, _transcript, source_id, **_kwargs):
            return MemoryAgentResult(
                conversation_id=source_id,
                rounds=1,
                touched=[],
                summary="truncated",
                truncated=True,
            )

    async def add_memory(
        transcript,
        _client_id,
        source_id,
        _user_id,
        _user_email,
        **kwargs,
    ):
        result = await service._run_agent_with_note_guarantee(
            TruncatedAgent,
            tmp_path / "test-user",
            transcript,
            source_id,
            source_date=kwargs["source_date"],
            source_duration_minutes=kwargs["source_duration_minutes"],
            source_title=kwargs["source_title"],
            source_people=kwargs["source_people"],
        )
        return True, result.touched

    service.add_memory = add_memory
    monkeypatch.setattr(memory_jobs, "Conversation", conversation_model)
    monkeypatch.setattr(
        memory_jobs,
        "get_user_by_id",
        AsyncMock(
            return_value=SimpleNamespace(email="test@example.com", primary_speakers=[])
        ),
    )
    monkeypatch.setattr(memory_jobs, "get_current_job", lambda: None)
    monkeypatch.setattr(memory_jobs, "get_memory_service", lambda: service)
    monkeypatch.setattr(memory_jobs, "publish_sse_event", Mock())
    monkeypatch.setattr(memory_jobs, "dispatch_plugin_event", AsyncMock())
    monkeypatch.setattr(
        "backend.services.memory.providers.chronicle.record_event_sync",
        lambda **event: system_events.append(event),
    )

    undecorated_job = memory_jobs.process_memory_job.__wrapped__.__wrapped__
    result = await undecorated_job(conversation_id, redis_client=None)

    assert result["success"] is True
    assert result["memories_created"] == 1
    assert system_events[0]["severity"] == "warning"
    assert system_events[0]["conversation_id"] == conversation_id
    assert system_events[0]["metadata"]["fallback_type"] == (
        "deterministic_source_preserving_note"
    )
    assert system_events[0]["metadata"]["reasons"] == [
        "invalid_note",
        "incomplete_agent",
    ]
    assert system_events[0]["metadata"]["primary_backend"] == "pi"
    assert system_events[0]["metadata"]["recovery_backend"] == "none"
    note = (
        tmp_path / "test-user" / "Conversations" / f"{conversation_id}.md"
    ).read_text(encoding="utf-8")
    assert 'people:\n  - "[[alex]]"' in note
