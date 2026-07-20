"""Completion guarantees for the agentic memory provider."""

from types import SimpleNamespace

import httpx
import openai
import pytest

from advanced_omi_backend import llm_client
from advanced_omi_backend.services.memory.agent.memory_agent import MemoryAgentResult
from advanced_omi_backend.services.memory.conversation_note import (
    ConversationNoteError,
    canonicalize_conversation_note,
    write_source_fallback_conversation_note,
)
from advanced_omi_backend.services.memory.providers import chronicle


class _Completions:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class _Operation:
    def __init__(self, completions, name):
        self.model_name = name
        self._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    def get_client(self, is_async=False):
        assert is_async is True
        return self._client

    def to_api_params(self):
        return {"model": self.model_name}


@pytest.mark.asyncio
async def test_tool_chat_uses_configured_fallback_on_context_overflow(monkeypatch):
    request = httpx.Request("POST", "http://local.test/v1/chat/completions")
    response = httpx.Response(400, request=request)
    error = openai.BadRequestError(
        "context overflow",
        response=response,
        body={
            "error": {
                "code": 400,
                "type": "exceed_context_size_error",
                "message": "request exceeds the available context size",
            }
        },
    )
    primary_calls = _Completions(error=error)
    expected = SimpleNamespace(choices=[])
    fallback_calls = _Completions(result=expected)
    primary = _Operation(primary_calls, "local")
    fallback = _Operation(fallback_calls, "fallback")
    registry = SimpleNamespace(
        get_llm_operation=lambda _name: primary,
        get_fallback_llm_operation=lambda _name, primary: fallback,
    )
    monkeypatch.setattr(llm_client, "get_models_registry", lambda: registry)

    result = await llm_client.async_chat_with_tools(
        [{"role": "user", "content": "long transcript"}],
        operation="memory_agent",
    )

    assert result is expected
    assert primary_calls.calls == 1
    assert fallback_calls.calls == 1


@pytest.mark.asyncio
async def test_memory_provider_retries_fallback_when_conversation_note_is_missing(
    monkeypatch, tmp_path
):
    user_root = tmp_path / "user"
    service = chronicle.MemoryService(SimpleNamespace())
    monkeypatch.setattr(service.vault, "user_root", lambda _user_id: user_root)
    monkeypatch.setattr(chronicle, "seed_vault_scaffold", lambda _root: None)

    recorded = []

    async def fake_record(*args, **kwargs):
        recorded.append((args, kwargs))

    monkeypatch.setattr(service, "_record_agent_touches", fake_record)
    calls = []

    class FakeMemoryAgent:
        def __init__(self, vault_root, force_fallback=False):
            self.vault_root = vault_root
            self.force_fallback = force_fallback

        async def run(self, _transcript, conversation_id, **_kwargs):
            calls.append(self.force_fallback)
            touched = []
            if self.force_fallback:
                note = self.vault_root / "Conversations" / f"{conversation_id}.md"
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text(
                    """---
categories:
  - "[[Conversations]]"
conversation_id: conversation-1
date: 2026-07-15T12:00:00+00:00
people: []
topics: []
duration_minutes: 2.5
---
## A useful conversation

### Summary
The speakers discussed a concrete plan for the project.

### Key Facts
- The project plan was reviewed.

### Action Items
- [ ] Follow up on the project plan.
""",
                    encoding="utf-8",
                )
                touched.append(f"Conversations/{conversation_id}.md")
            return MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=1,
                touched=touched,
                summary="done",
            )

    import advanced_omi_backend.services.memory.agent as agent_module

    monkeypatch.setattr(agent_module, "MemoryAgent", FakeMemoryAgent)

    success, touched = await service._add_memory_agent(
        "Speaker: enough transcript text",
        "conversation-1",
        "user-1",
        source_date="2026-07-15T12:00:00+00:00",
        source_duration_minutes=2.5,
        source_title="A useful conversation",
    )

    assert success is True
    assert calls == [False, True]
    assert touched == ["Conversations/conversation-1.md"]
    assert len(recorded) == 1


def test_conversation_note_canonicalization_rejects_placeholder_content(tmp_path):
    note = tmp_path / "conversation.md"
    note.write_text(
        """---
categories: ["[[Conversations]]"]
conversation_id: wrong-id
date: 2026-07-16
people: []
topics: []
duration_minutes: 0
---
## Untitled

### Summary

### Key Facts
-

### Action Items
- [ ]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConversationNoteError):
        canonicalize_conversation_note(
            note,
            conversation_id="conversation-1",
            date="2026-07-15T12:00:00+00:00",
            duration_minutes=2.5,
            title="Source title",
        )


def test_conversation_note_canonicalization_uses_trusted_metadata(tmp_path):
    note = tmp_path / "conversation.md"
    note.write_text(
        """## Model supplied title
---
categories: ["[[Conversations]]"]
conversation_id: hallucinated
date: 2026-07-16
people: ["[[Ankush]]", "[[Unknown Speaker 4]]", "[[Hermes]]"]
topics: ["[[Memory systems]]"]
duration_minutes: 999
---

### Summary
The conversation covered a reliable memory rebuild process.

### Key Facts
- The rebuild must preserve source metadata.
- The rebuild must preserve source metadata.

### Action Items
- [ ] Run a canary before the full rebuild.
""",
        encoding="utf-8",
    )

    canonicalize_conversation_note(
        note,
        conversation_id="conversation-1",
        date="2026-07-15T12:00:00+00:00",
        duration_minutes=2.5,
        title="Trusted source title",
    )

    content = note.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert 'conversation_id: "conversation-1"' in content
    assert 'date: "2026-07-15T12:00:00+00:00"' in content
    assert "duration_minutes: 2.5" in content
    assert "## Model supplied title" in content
    assert "Unknown Speaker" not in content
    assert 'people:\n  - "[[Ankush]]"' in content
    assert '  - "[[Hermes]]"' in content
    assert content.count("- The rebuild must preserve source metadata.") == 1


def test_source_fallback_preserves_short_transcript(tmp_path):
    note = tmp_path / "conversation.md"
    write_source_fallback_conversation_note(
        note,
        transcript="Hey Hermes, why does it only work during the demo?",
        conversation_id="conversation-1",
        date="2026-07-15T12:00:00+00:00",
        duration_minutes=0.2,
        title="Hermes Discussion",
    )

    canonicalize_conversation_note(
        note,
        conversation_id="conversation-1",
        date="2026-07-15T12:00:00+00:00",
        duration_minutes=0.2,
        title="Hermes Discussion",
    )
    assert "why does it only work during the demo" in note.read_text(encoding="utf-8")
