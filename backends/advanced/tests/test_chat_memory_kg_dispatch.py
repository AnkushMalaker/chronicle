"""Unit tests for chat memory plugin dispatch.

Verifies that ChatService.extract_memories_from_session triggers
MEMORY_PROCESSED plugin dispatch on the success branch.

LLM-independent: the memory_service.add_memory call is mocked to return
success without actually contacting an LLM.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from advanced_omi_backend.chat_service import ChatMessage, ChatService


@pytest.mark.asyncio
async def test_extract_memories_triggers_plugin_dispatch():
    """Success branch must dispatch the memory-processed plugin event."""
    cs = ChatService()

    # Stub _initialized to skip real init; provide collection mocks for the methods we hit.
    cs._initialized = True
    cs.sessions_collection = AsyncMock()
    cs.sessions_collection.find_one = AsyncMock(
        return_value={
            "session_id": "sess-x",
            "user_id": "user-x",
            "user_email": "user-x@example.com",
            "title": "smoke",
        }
    )

    fake_messages = [
        ChatMessage(
            message_id="m1",
            session_id="sess-x",
            user_id="user-x",
            role="user",
            content="I had lunch with Aru at Blue Tokai.",
            timestamp=datetime.now(timezone.utc),
        ),
        ChatMessage(
            message_id="m2",
            session_id="sess-x",
            user_id="user-x",
            role="assistant",
            content="Sounds nice.",
            timestamp=datetime.now(timezone.utc),
        ),
    ]
    cs.get_session_messages = AsyncMock(return_value=fake_messages)

    cs.memory_service = AsyncMock()
    cs.memory_service.add_memory = AsyncMock(return_value=(True, ["mem-1", "mem-2"]))
    cs.memory_service.provider_identifier = "chronicle"

    with patch(
        "advanced_omi_backend.chat_service.dispatch_plugin_event",
        new_callable=AsyncMock,
    ) as fake_dispatch:
        ok, ids, count = await cs.extract_memories_from_session(
            session_id="sess-x", user_id="user-x"
        )

    assert ok is True
    assert ids == ["mem-1", "mem-2"]
    assert count == 2

    fake_dispatch.assert_awaited_once()
    dispatch_kwargs = fake_dispatch.await_args.kwargs
    assert dispatch_kwargs["event"].name == "MEMORY_PROCESSED"
    assert dispatch_kwargs["user_id"] == "user-x"
    assert dispatch_kwargs["data"]["memory_count"] == 2
    assert dispatch_kwargs["data"]["conversation_id"] == "chat_sess-x"
    assert dispatch_kwargs["data"]["conversation"]["client_id"] == "chat_interface"
    assert dispatch_kwargs["metadata"]["memory_provider"] == "chronicle"


@pytest.mark.asyncio
async def test_no_memories_still_dispatches_success():
    """Successful extraction with no new IDs should still dispatch."""
    cs = ChatService()
    cs._initialized = True
    cs.sessions_collection = AsyncMock()
    cs.sessions_collection.find_one = AsyncMock(
        return_value={"session_id": "sess-x", "user_id": "user-x"}
    )
    cs.get_session_messages = AsyncMock(
        return_value=[
            ChatMessage(
                message_id="m1",
                session_id="sess-x",
                user_id="user-x",
                role="user",
                content="hello",
                timestamp=datetime.now(timezone.utc),
            ),
            ChatMessage(
                message_id="m2",
                session_id="sess-x",
                user_id="user-x",
                role="assistant",
                content="hi",
                timestamp=datetime.now(timezone.utc),
            ),
        ]
    )
    cs.memory_service = AsyncMock()
    cs.memory_service.add_memory = AsyncMock(return_value=(True, []))
    cs.memory_service.provider_identifier = "chronicle"

    with patch(
        "advanced_omi_backend.chat_service.dispatch_plugin_event",
        new_callable=AsyncMock,
    ) as fake_dispatch:
        ok, ids, count = await cs.extract_memories_from_session(
            session_id="sess-x", user_id="user-x"
        )

    assert ok is True
    assert ids == []
    assert count == 0

    fake_dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_failure_is_non_fatal():
    """If plugin dispatch raises, extract_memories_from_session must still return success."""
    cs = ChatService()
    cs._initialized = True
    cs.sessions_collection = AsyncMock()
    cs.sessions_collection.find_one = AsyncMock(
        return_value={"session_id": "sess-x", "user_id": "user-x"}
    )
    cs.get_session_messages = AsyncMock(
        return_value=[
            ChatMessage(
                message_id="m1",
                session_id="sess-x",
                user_id="user-x",
                role="user",
                content="hello",
                timestamp=datetime.now(timezone.utc),
            ),
            ChatMessage(
                message_id="m2",
                session_id="sess-x",
                user_id="user-x",
                role="assistant",
                content="hi",
                timestamp=datetime.now(timezone.utc),
            ),
        ]
    )
    cs.memory_service = AsyncMock()
    cs.memory_service.add_memory = AsyncMock(return_value=(True, ["m"]))
    cs.memory_service.provider_identifier = "chronicle"

    with patch(
        "advanced_omi_backend.chat_service.dispatch_plugin_event",
        new_callable=AsyncMock,
        side_effect=RuntimeError("plugin router down"),
    ):
        ok, _ids, count = await cs.extract_memories_from_session(
            session_id="sess-x", user_id="user-x"
        )

    assert ok is True
    assert count == 1


@pytest.mark.asyncio
async def test_failed_extraction_does_not_dispatch():
    """If memory_service.add_memory returns False, neither KG nor dispatch must fire."""
    cs = ChatService()
    cs._initialized = True
    cs.sessions_collection = AsyncMock()
    cs.sessions_collection.find_one = AsyncMock(
        return_value={"session_id": "sess-x", "user_id": "user-x"}
    )
    cs.get_session_messages = AsyncMock(
        return_value=[
            ChatMessage(
                message_id="m1",
                session_id="sess-x",
                user_id="user-x",
                role="user",
                content="hello",
                timestamp=datetime.now(timezone.utc),
            ),
            ChatMessage(
                message_id="m2",
                session_id="sess-x",
                user_id="user-x",
                role="assistant",
                content="hi",
                timestamp=datetime.now(timezone.utc),
            ),
        ]
    )
    cs.memory_service = AsyncMock()
    cs.memory_service.add_memory = AsyncMock(return_value=(False, []))

    with patch(
        "advanced_omi_backend.chat_service.dispatch_plugin_event",
        new_callable=AsyncMock,
    ) as fake_dispatch:
        ok, _ids, _count = await cs.extract_memories_from_session(
            session_id="sess-x", user_id="user-x"
        )

    assert ok is False
    fake_dispatch.assert_not_awaited()
