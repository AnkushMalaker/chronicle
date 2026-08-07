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


@pytest.mark.asyncio
async def test_day_write_fails_when_no_backend_completes(tmp_path, monkeypatch):
    """A pre-existing Daily note must not be mistaken for this run's work.

    Both backends raising leaves nothing recorded, but the day note for that date can
    already exist from an earlier write. Reporting success then latches the day as
    `written` with an empty vault and it is never retried.
    """

    service = MemoryService(
        SimpleNamespace(write_agent_backend="pi", write_recovery_backend="direct")
    )
    local_date = "2026-08-05"
    day_note = tmp_path / "user-one" / "Daily" / f"{local_date}.md"
    day_note.parent.mkdir(parents=True, exist_ok=True)
    day_note.write_text("# Written by an earlier run\n", encoding="utf-8")

    class FailingAgent:
        def __init__(self, _root):
            pass

        async def run(self, *_args, **_kwargs):
            raise RuntimeError("RateLimitError")

    monkeypatch.setattr(service, "_write_agent_class", lambda: FailingAgent)
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: FailingAgent)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: tmp_path / "user-one")

    success, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-05T00:00:00+05:30",
    )

    assert success is False
    assert touched == []
    # The earlier run's note is left alone; failure means unwritten, not clobbered.
    assert day_note.read_text(encoding="utf-8") == "# Written by an earlier run\n"


@pytest.mark.asyncio
async def test_day_write_fails_when_only_other_notes_were_touched(
    tmp_path, monkeypatch
):
    """A pre-existing Daily note must not be mistaken for this run writing the day.

    Every date already has a Daily note from the retired per-observation curation, so
    an existence check passes for a run that edited some People notes and never
    recorded the day. Two of four backfilled days reported success exactly that way.
    """

    service = MemoryService(SimpleNamespace(write_recovery_backend=None))
    local_date = "2026-08-06"
    root = tmp_path / "user-one"
    day_note = root / "Daily" / f"{local_date}.md"
    day_note.parent.mkdir(parents=True, exist_ok=True)
    day_note.write_text(
        "# Left by the old per-observation curation\n", encoding="utf-8"
    )

    class TouchesOtherNotesOnly:
        def __init__(self, _root):
            pass

        async def run(self, *_args, **_kwargs):
            return MemoryAgentResult(
                conversation_id=local_date,
                rounds=1,
                touched=["People/Vatsal.md"],
                summary="updated a person note but never wrote the day",
            )

    monkeypatch.setattr(service, "_write_agent_class", lambda: TouchesOtherNotesOnly)
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: None)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: root)

    success, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-06T00:00:00+05:30",
    )

    assert success is False
    # The People edit is still reported, so the audit ledger keeps what did happen.
    assert "People/Vatsal.md" in touched


@pytest.mark.asyncio
async def test_day_write_treats_a_deliberate_no_op_as_done_without_recovery(
    tmp_path, monkeypatch
):
    """Deciding nothing needs recording is an outcome, not a failure to recover from.

    Every date already carries 65-177 entries from the retired per-observation
    curation, so an agent told not to duplicate what a note already holds correctly
    writes nothing. Running the recovery backend then only reaches the same
    conclusion a second time, and reporting failure retries the day until it is
    skipped.
    """

    service = MemoryService(SimpleNamespace(write_recovery_backend="direct"))
    local_date = "2026-08-06"
    root = tmp_path / "user-one"
    (root / "Daily").mkdir(parents=True, exist_ok=True)
    (root / "Daily" / f"{local_date}.md").write_text("# Already recorded\n", "utf-8")
    recovery_calls = []

    class DecidesNothingToAdd:
        def __init__(self, _root):
            pass

        async def run(self, *_args, **_kwargs):
            return MemoryAgentResult(
                conversation_id=local_date,
                rounds=6,
                touched=[],
                summary="The day is already recorded; nothing new to add.",
            )

    class Recovery:
        def __init__(self, _root):
            recovery_calls.append(1)

        async def run(self, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("recovery must not run for a deliberate no-op")

    monkeypatch.setattr(service, "_write_agent_class", lambda: DecidesNothingToAdd)
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: Recovery)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: root)

    success, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-06T00:00:00+05:30",
    )

    assert success is True
    assert touched == []
    assert recovery_calls == []
