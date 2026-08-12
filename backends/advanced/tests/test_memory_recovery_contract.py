"""Provider-owned recovery and lossless source guarantees."""

import logging
from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory.agent.memory_agent import MemoryAgentResult
from advanced_omi_backend.services.memory.base import DayWriteOutcome
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
    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=False,
        )
    )

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
    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=False,
        )
    )

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
    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend="direct",
            review_writes=False,
        )
    )
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
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend="direct",
            review_writes=False,
        )
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

    outcome, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-05T00:00:00+05:30",
    )

    assert outcome is DayWriteOutcome.FAILED
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

    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=False,
        )
    )
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

    outcome, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-06T00:00:00+05:30",
    )

    assert outcome is DayWriteOutcome.FAILED
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

    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend="direct",
            review_writes=False,
        )
    )
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
                # It checked before concluding, which is what makes this deliberate.
                verified=True,
            )

    class Recovery:
        def __init__(self, _root):
            recovery_calls.append(1)

        async def run(self, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("recovery must not run for a deliberate no-op")

    monkeypatch.setattr(service, "_write_agent_class", lambda: DecidesNothingToAdd)
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: Recovery)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: root)

    outcome, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-06T00:00:00+05:30",
    )

    assert outcome is DayWriteOutcome.COMPLETE
    assert touched == []
    assert recovery_calls == []


@pytest.mark.asyncio
async def test_day_write_surfaces_nonfatal_agent_diagnostics(
    tmp_path, monkeypatch, caplog
):
    """A completed mutation must not hide the agent errors behind a numeric count."""

    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=False,
        )
    )
    local_date = "2026-08-06"
    root = tmp_path / "user-one"
    day_rel = f"Daily/{local_date}.md"

    class WritesWithRecoveredDiagnostic:
        def __init__(self, _root):
            pass

        async def run(self, *_args, **_kwargs):
            note = root / day_rel
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(
                f"# {local_date}\n\n## Episodes\n\n- Recovered write.\n",
                encoding="utf-8",
            )
            return MemoryAgentResult(
                conversation_id=local_date,
                rounds=3,
                touched=[day_rel],
                summary="Recorded the day after a retry.",
                errors=["recovered after synthetic Pi retry"],
                verified=True,
            )

    monkeypatch.setattr(
        service, "_write_agent_class", lambda: WritesWithRecoveredDiagnostic
    )
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: None)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: root)
    caplog.set_level(logging.WARNING, logger="memory_service")

    outcome, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-06T00:00:00+05:30",
    )

    assert outcome is DayWriteOutcome.COMPLETE
    assert touched == [day_rel]
    assert "recovered after synthetic Pi retry" in caplog.text


@pytest.mark.asyncio
async def test_day_write_reconciles_stale_episode_ranges_before_reporting_success(
    tmp_path, monkeypatch
):
    """Appending a new episode is not enough when an older run had wrong bounds.

    The episode index is a source-backed contract, not a judgement call, so it is
    reconciled deterministically from the digest rather than by asking the model to
    repair itself. What matters is that a run which wrote a stale bound cannot report
    success while the note still carries it — not how many agent rounds that took.
    Asserting a second guidance round here would pin the old mechanism and would make
    the cheaper deterministic fix look like a regression.
    """

    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=False,
        )
    )
    local_date = "2026-08-10"
    root = tmp_path / "user-one"
    day_rel = f"Daily/{local_date}.md"
    rounds_run = []
    digest = """Local day 2026-08-10 (Etc/UTC), 2 episode(s).

### 06:10–06:52 · meeting · highlight
title: ADS Weekly Planning Sync

### 21:46–22:09 · application_state · background
title: Late Zed review
"""

    class WritesAStaleBound:
        def __init__(self, _root):
            pass

        async def run(self, *_args, guidance="", **_kwargs):
            rounds_run.append(guidance)
            note = root / day_rel
            note.parent.mkdir(parents=True, exist_ok=True)
            # Wrong end bound on the first episode: the shape an earlier run leaves
            # behind when it appends a new episode and keeps every stale range.
            note.write_text(
                f"# {local_date}\n\n## Episodes\n\n"
                + "".join(
                    f"- {value} · episode — summary\n"
                    for value in ("06:10–06:10", "21:46–22:09")
                ),
                encoding="utf-8",
            )
            return MemoryAgentResult(
                conversation_id=local_date,
                rounds=3,
                touched=[day_rel],
                summary="Recorded the day.",
                verified=True,
            )

    monkeypatch.setattr(service, "_write_agent_class", lambda: WritesAStaleBound)
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: None)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: root)

    outcome, touched = await service._add_day_memory_agent(
        digest,
        local_date,
        "user-one",
        source_date="2026-08-10T00:00:00+00:00",
    )

    written = (root / day_rel).read_text(encoding="utf-8")
    assert outcome is DayWriteOutcome.COMPLETE
    assert touched == [day_rel]
    # The index mirrors the digest exactly: the stale bound is gone, both source
    # ranges are present, and they are in chronological order.
    assert "06:10–06:52" in written
    assert "06:10–06:10" not in written
    assert written.index("06:10–06:52") < written.index("21:46–22:09")
    # Reconciliation is deterministic, so it costs no extra agent round.
    assert rounds_run == [""]


@pytest.mark.asyncio
async def test_partial_day_write_logs_limit_cause_and_work_done(
    tmp_path, monkeypatch, caplog
):
    """A preserved partial write must report why it did not latch as complete.

    The run wrote a structurally valid day note and then hit its round limit, so it is
    neither complete nor failed: reporting it complete hides that it may never have
    reached its People/Topic edits, and reporting it failed spends the retry budget
    re-reaching the same limit before settling the day as ``skipped``.
    """

    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=False,
        )
    )
    local_date = "2026-08-07"
    root = tmp_path / "user-one"
    day_rel = f"Daily/{local_date}.md"

    class StopsAtRoundLimit:
        def __init__(self, _root):
            pass

        async def run(self, *_args, **_kwargs):
            note = root / day_rel
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(
                f"# {local_date}\n\n## Episodes\n\n- Partial write.\n",
                encoding="utf-8",
            )
            return MemoryAgentResult(
                conversation_id=local_date,
                rounds=32,
                touched=[day_rel],
                summary="Stopped at the configured limit.",
                tool_calls=35,
                errors=["Pi tool-round limit exceeded (32)"],
                truncated=True,
            )

    monkeypatch.setattr(service, "_write_agent_class", lambda: StopsAtRoundLimit)
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: None)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: root)
    caplog.set_level(logging.ERROR, logger="memory_service")

    outcome, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-07T00:00:00+05:30",
    )

    assert outcome is DayWriteOutcome.PARTIAL
    assert touched == [day_rel]
    # The partial mutations are kept, not discarded.
    assert (root / day_rel).read_text(encoding="utf-8").strip()
    # And the cause is reported at ERROR, with the work actually done, so a day that
    # keeps landing here is visible rather than quietly half-recorded.
    assert "rounds=32 tools=35" in caplog.text
    assert "Pi tool-round limit exceeded (32)" in caplog.text


@pytest.mark.asyncio
async def test_narrating_the_next_step_is_not_a_deliberate_no_op(tmp_path, monkeypatch):
    """Stopping mid-thought must reach recovery, not pass as "nothing to record".

    Qwen3.6 and DeepSeek V4 Pro both end runs by narrating the next tool call as prose
    instead of emitting it — a known Qwen3.6 tool-calling defect, and observed on
    DeepSeek here too. The result is a clean-looking finish: no error, no truncation,
    no edits, and a perfectly well-formed summary that happens to be a sentence about
    what the model was *about* to do. The one thing it never did was verify.
    """

    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend="direct",
            review_writes=False,
        )
    )
    local_date = "2026-08-06"
    root = tmp_path / "user-one"
    (root / "Daily").mkdir(parents=True, exist_ok=True)
    (root / "Daily" / f"{local_date}.md").write_text("# From an older run\n", "utf-8")
    recovery_calls = []

    class StopsMidThought:
        def __init__(self, _root):
            pass

        async def run(self, *_args, **_kwargs):
            return MemoryAgentResult(
                conversation_id=local_date,
                rounds=7,
                touched=[],
                summary=(
                    "Let me check the later parts of the day note for evening episodes"
                ),
                verified=False,
            )

    class Recovery:
        def __init__(self, _root):
            recovery_calls.append(1)

        async def run(self, *_args, **_kwargs):
            return MemoryAgentResult(
                conversation_id=local_date,
                rounds=4,
                touched=[f"Daily/{local_date}.md"],
                summary="Recorded the day.",
                verified=True,
            )

    monkeypatch.setattr(service, "_write_agent_class", lambda: StopsMidThought)
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: Recovery)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: root)

    outcome, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-06T00:00:00+05:30",
    )

    assert recovery_calls == [1]
    assert outcome is DayWriteOutcome.COMPLETE
    assert f"Daily/{local_date}.md" in touched


@pytest.mark.asyncio
async def test_a_reviewer_finding_sends_the_write_back_for_repair(
    tmp_path, monkeypatch
):
    """A well-formed duplicate passes structural verification and must still be caught.

    This is the DeepSeek failure end to end: the day note is written, every rule in
    vault_verify is satisfied, and the run would be accepted — until a second agent
    reads what was added and says the vault already had it.
    """

    service = MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=True,
        )
    )
    local_date = "2026-08-06"
    root = tmp_path / "user-one"
    day_rel = f"Daily/{local_date}.md"
    repairs = []

    class Writes:
        def __init__(self, _root):
            pass

        async def run(self, *_args, guidance="", **_kwargs):
            if guidance:
                repairs.append(guidance)
            note = root / day_rel
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("## 11:41 Standup\n- shipped it\n", encoding="utf-8")
            return MemoryAgentResult(
                conversation_id=local_date,
                rounds=3,
                touched=[day_rel],
                summary="Recorded the day.",
                verified=True,
            )

    async def fake_review(_root, **_kwargs):
        return SimpleNamespace(
            findings=[
                SimpleNamespace(
                    path=day_rel,
                    rule="redundant",
                    detail="'- shipped it' is already recorded in the 11:20 entry",
                    render=lambda: f"- {day_rel} [redundant]: already recorded",
                )
            ],
            reported=True,
            rounds=4,
            tool_calls=6,
            warnings=[],
        )

    monkeypatch.setattr(service, "_write_agent_class", lambda: Writes)
    monkeypatch.setattr(service, "_recovery_agent_class", lambda: None)
    monkeypatch.setattr(service.vault, "user_root", lambda _uid: root)
    monkeypatch.setattr(
        "advanced_omi_backend.services.memory.agent.review_agent.review_vault_write",
        fake_review,
    )

    outcome, touched = await service._add_day_memory_agent(
        "A day digest long enough to clear the minimum-length guard.",
        local_date,
        "user-one",
        source_date="2026-08-06T00:00:00+05:30",
    )

    # The day was written, so the run succeeds — a redundancy is a blemish on a real
    # record, not a reason to throw the day away and retry it until it is skipped.
    assert outcome is DayWriteOutcome.COMPLETE
    assert day_rel in touched
    # But it went back for repair, and the instruction names the remedy: a write agent
    # told to record things does not read "fix this" as "delete this".
    assert len(repairs) == 1
    assert "DELETING the line" in repairs[0]
