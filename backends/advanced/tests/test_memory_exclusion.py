"""Regression coverage for conversations excluded from user memory."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from advanced_omi_backend.workers import memory_jobs


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
