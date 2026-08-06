"""Provider-owned recovery and lossless source guarantees."""

from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory.agent.memory_agent import MemoryAgentResult
from advanced_omi_backend.services.memory.conversation_note import (
    write_source_fallback_conversation_note,
)
from advanced_omi_backend.services.memory.providers.chronicle import MemoryService
from advanced_omi_backend.services.memory.telemetry import current_memory_attempt


def _write_partial_valid_note(root, conversation_id):
    note = root / "Conversations" / f"{conversation_id}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        """---
categories:
  - "[[Conversations]]"
conversation_id: "conversation-partial"
date: "2026-08-06T12:00:00+00:00"
people: []
topics: []
duration_minutes: 1
---
## Partial attempt

### Summary
Only one early detail was recorded before the agent stopped.

### Key Facts
- The attempt did not preserve the complete source.

### Action Items
- [ ]
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_incomplete_valid_note_is_replaced_by_lossless_source_fallback(tmp_path):
    source = "Speaker: retain this exact ending after the valid partial note stops."
    service = MemoryService(SimpleNamespace(write_recovery_backend=None))

    class IncompleteAgent:
        def __init__(self, root):
            self.root = root

        async def run(self, _transcript, conversation_id, **_kwargs):
            _write_partial_valid_note(self.root, conversation_id)
            return MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=1,
                touched=[f"Conversations/{conversation_id}.md"],
                summary="stopped",
                truncated=True,
            )

    result = await service._run_agent_with_note_guarantee(
        IncompleteAgent,
        tmp_path,
        source,
        "conversation-partial",
        source_date="2026-08-06T12:00:00+00:00",
        source_duration_minutes=1,
        source_title="Partial attempt",
    )

    note_text = (tmp_path / "Conversations" / "conversation-partial.md").read_text(
        encoding="utf-8"
    )
    assert source in note_text
    assert "Verbatim source transcript" in note_text
    assert result.truncated is True
    assert result.touched == ["Conversations/conversation-partial.md"]


@pytest.mark.asyncio
async def test_provider_exception_diagnostics_never_persist_arbitrary_text(tmp_path):
    service = MemoryService(SimpleNamespace(write_recovery_backend=None))

    class FailingAgent:
        def __init__(self, _root):
            pass

        async def run(self, *_args, **_kwargs):
            raise RuntimeError(
                "request failed at https://bearer-secret@example.invalid/v1"
            )

    result = await service._run_agent_with_note_guarantee(
        FailingAgent,
        tmp_path,
        "Speaker: preserve this source despite the provider exception.",
        "conversation-error",
        source_date="2026-08-06T12:00:00+00:00",
        source_duration_minutes=1,
        source_title="Provider error",
    )

    serialized = " ".join(result.errors)
    assert serialized == "primary write backend failed (RuntimeError)"
    assert "bearer-secret" not in serialized
    assert "example.invalid" not in serialized
    assert "preserve this source" in (
        tmp_path / "Conversations" / "conversation-error.md"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_provider_labels_primary_and_recovery_attempts(tmp_path, monkeypatch):
    service = MemoryService(SimpleNamespace(write_recovery_backend="direct"))
    observed_attempts = []

    class PrimaryAgent:
        def __init__(self, _root):
            observed_attempts.append(("primary_init", current_memory_attempt()))

        async def run(self, *_args, **_kwargs):
            observed_attempts.append(("primary_run", current_memory_attempt()))
            raise RuntimeError("synthetic primary failure")

    class RecoveryAgent:
        def __init__(self, root, *, force_fallback=False):
            assert force_fallback is False
            self.root = root
            observed_attempts.append(("recovery_init", current_memory_attempt()))

        async def run(self, transcript, conversation_id, **kwargs):
            observed_attempts.append(("recovery_run", current_memory_attempt()))
            write_source_fallback_conversation_note(
                self.root / "Conversations" / f"{conversation_id}.md",
                transcript=transcript,
                conversation_id=conversation_id,
                date=kwargs["date"],
                duration_minutes=kwargs["duration_minutes"],
                title=kwargs["title"],
            )
            return MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=1,
                touched=[f"Conversations/{conversation_id}.md"],
                summary="recovered",
            )

    monkeypatch.setattr(service, "_recovery_agent_class", lambda: RecoveryAgent)

    result = await service._run_agent_with_note_guarantee(
        PrimaryAgent,
        tmp_path,
        "Speaker: this synthetic source is long enough to preserve.",
        "conversation-attempts",
        source_date="2026-08-06T12:00:00+00:00",
        source_duration_minutes=1,
        source_title="Attempt labels",
    )

    assert observed_attempts == [
        ("primary_init", "primary"),
        ("primary_run", "primary"),
        ("recovery_init", "recovery"),
        ("recovery_run", "recovery"),
    ]
    assert result.truncated is False
    assert current_memory_attempt() == "primary"
