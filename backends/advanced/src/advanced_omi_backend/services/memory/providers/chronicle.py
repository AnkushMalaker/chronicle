"""Chronicle memory service — agentic Markdown vault.

This module provides the core MemoryService class that maintains a personal,
Obsidian-style Markdown vault (``data/conversation_docs/<user>/``) as the single
source of truth for a user's memory:

- **Write** — a tool-calling memory agent records each conversation and surgically
  edits the People/Topic/Category notes it touches (``_add_memory_agent``).
- **Read** — a read-only retrieval agent drives ripgrep over the vault, reads the
  relevant notes, and synthesises an answer (``_search_vault_grep``).

The vault is the only store; there is no separate search index. All knowledge about
how the vault is shaped lives in the memory agent's prompts (see ``..agent``).
"""

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from ..audit import record_vault_change
from ..base import MemoryEntry, MemoryServiceBase, VaultSearchUnavailable
from ..config import MemoryConfig
from ..conversation_note import (
    ConversationNoteError,
    canonicalize_conversation_note,
    write_source_fallback_conversation_note,
)
from ..telemetry import (
    memory_attempt,
    memory_span,
    set_observation_io,
    set_safe_span_attributes,
    text_payload,
)
from ..vault_manager import ConvDocVaultManager
from ..vault_scaffold import is_scaffold_note, seed_vault_scaffold
from ..vault_verify import render_findings, verify_vault_changes

memory_logger = logging.getLogger("memory_service")


def _safe_exception_diagnostic(exc: BaseException) -> str:
    """Return a useful diagnostic without persisting arbitrary provider text."""

    return type(exc).__name__


class MemoryService(MemoryServiceBase):
    """Memory service backed by an agentic Markdown vault (the ground truth).

    Each conversation is recorded into the vault by the memory agent, which also
    updates the person/topic/category notes it mentions. Retrieval is an agentic
    ripgrep over those notes that returns a synthesised answer plus the notes it read.
    """

    @property
    def provider_identifier(self) -> str:
        return "chronicle"

    def __init__(self, config: MemoryConfig):
        super().__init__()
        self.config = config
        self.vault = ConvDocVaultManager()

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._validate_configured_backends()
        # Vault-only: the agent generates + writes notes via its own tool-calling
        # LLM, and search greps the vault. No external index to connect to.
        self._initialized = True
        memory_logger.info("✅ Chronicle memory service initialized (agentic vault).")

    def _validate_configured_backends(self) -> None:
        """Resolve executors and model contracts before reporting readiness."""
        # A missing Pi/Codex runtime must not silently turn an experiment into a
        # different backend. Recovery remains available for a runtime that disappears
        # after this check, inside `_run_agent_with_note_guarantee`.
        write_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        write_class = self._write_agent_class(write_backend)
        if write_backend == "pi":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.pi_agent import validate_pi_executor_config

            validate_pi_executor_config("memory_write")
        elif write_backend == "codex":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.codex_agent import validate_codex_executor_config

            validate_codex_executor_config()

        recovery_backend = getattr(self.config, "write_recovery_backend", "direct")
        if recovery_backend:
            recovery_backend = recovery_backend.lower()
            recovery_class = self._write_agent_class(recovery_backend)
            if recovery_backend == "pi":
                # Lazy: ..agent imports llm_client, which imports this package's config back.
                from ..agent.pi_agent import validate_pi_executor_config

                validate_pi_executor_config(
                    "memory_write", force_fallback=recovery_class is write_class
                )
            elif recovery_backend == "codex":
                # Lazy: ..agent imports llm_client, which imports this package's config back.
                from ..agent.codex_agent import validate_codex_executor_config

                validate_codex_executor_config()

        search_backend = (
            getattr(self.config, "search_agent_backend", "direct") or "direct"
        ).lower()
        if search_backend == "pi":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.pi_agent import validate_pi_executor_config

            validate_pi_executor_config("memory_search")
        elif search_backend != "direct":
            raise ValueError(f"Unsupported memory search backend: {search_backend}")

    def _write_agent_class(self, backend: Optional[str] = None):
        """Resolve one configured write backend to its agent implementation.

        Configuration errors are deliberately loud. The explicitly configured
        recovery backend handles failed note creation; availability is not a reason
        to change the primary backend implicitly.
        """
        # Lazy import: circular dependency (agent → memory_agent → llm_client →
        # services.memory.config → service_factory → this module)
        from ..agent import MemoryAgent

        name = (
            backend or getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        if name == "direct":
            return MemoryAgent
        if name == "codex":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.codex_agent import CodexMemoryAgent, codex_executor_available

            available, detail = codex_executor_available()
            if available:
                return CodexMemoryAgent
            raise RuntimeError(
                "memory write backend 'codex' is unavailable: " f"{detail}"
            )
        if name == "pi":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.pi_agent import PiMemoryAgent, pi_executor_available

            available, detail = pi_executor_available()
            if available:
                return PiMemoryAgent
            raise RuntimeError("memory write backend 'pi' is unavailable: " f"{detail}")
        raise ValueError(f"Unsupported memory write backend: {name}")

    def _recovery_agent_class(self):
        backend = getattr(self.config, "write_recovery_backend", "direct")
        return self._write_agent_class(backend) if backend else None

    # =========================================================================
    # ADD MEMORY
    # =========================================================================

    async def add_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        allow_update: bool = False,
        db_helper: Any = None,
        *,
        source_date: Optional[str] = None,
        source_duration_minutes: Optional[float] = None,
        source_title: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        write_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        recovery_backend = getattr(self.config, "write_recovery_backend", None)
        with memory_span(
            "memory_write",
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": source_id,
                "session.id": source_id,
                "langfuse.session.id": source_id,
                "chronicle.user_id": str(user_id),
                "langfuse.user.id": str(user_id),
                "chronicle.client_id": client_id,
                "chronicle.pipeline.stage": "memory_write",
                "chronicle.memory.operation": "write",
                "chronicle.memory.primary_backend": write_backend,
                "chronicle.memory.recovery_backend": recovery_backend or "none",
                "chronicle.memory.transcript_chars": len(transcript or ""),
            },
        ) as span:
            set_observation_io(
                span,
                input={
                    "conversation_id": source_id,
                    "transcript": text_payload(transcript),
                    "title": text_payload(source_title),
                    "duration_minutes": source_duration_minutes,
                },
            )
            await self._ensure_initialized()
            success, touched = await self._add_memory_agent(
                transcript,
                source_id,
                user_id,
                source_date=source_date,
                source_duration_minutes=source_duration_minutes,
                source_title=source_title,
            )
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": success,
                    "chronicle.memory.touched_count": len(touched),
                },
            )
            set_observation_io(
                span,
                output={"success": success, "touched_count": len(touched)},
            )
            return success, touched

    async def _add_memory_agent(
        self,
        transcript: str,
        source_id: str,
        user_id: str,
        *,
        source_date: Optional[str] = None,
        source_duration_minutes: Optional[float] = None,
        source_title: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Write path via the tool-calling memory agent.

        The agent creates the conversation note and surgically edits person/topic
        notes in the user's vault. Returns ``(success, touched_paths)`` — the touched
        vault-relative note paths stand in for the chunk/memory ids the older index
        path returned, so the existing job bookkeeping (counts, versions) works unchanged.
        """
        if not transcript or len(transcript.strip()) < 10:
            memory_logger.info(f"Skipping empty transcript for {source_id}")
            return True, []

        t0 = time.perf_counter()
        user_root = self.vault.user_root(user_id)
        # Concurrent memory jobs for the same user may run on different RQ workers;
        # each individual vault mutation is serialised inside VaultTools via the
        # per-user vault_note_lock (lock-write-unlock, never across LLM calls).
        seed_vault_scaffold(user_root)  # idempotent: .base + hub notes
        existing_before = self._vault_note_set(user_root)
        result = await self._run_agent_with_note_guarantee(
            None,
            user_root,
            transcript,
            source_id,
            source_date=source_date,
            source_duration_minutes=source_duration_minutes,
            source_title=source_title,
        )
        if (result.truncated or result.stalled) and not result.touched:
            reason = (
                "truncated LLM response" if result.truncated else "stalled retry loop"
            )
            memory_logger.error(
                "❌ add_memory(agent) %s: aborted on %s after %d rounds (%d tool "
                "calls) — nothing recorded (%.2fs)",
                source_id,
                reason,
                result.rounds,
                result.tool_calls,
                time.perf_counter() - t0,
            )
            return False, []
        await self._record_agent_touches(
            user_id,
            source_id,
            user_root,
            result.touched,
            existing_before,
            removed=result.removed,
        )
        expected_note = user_root / "Conversations" / f"{Path(source_id).name}.md"
        if not expected_note.is_file():
            memory_logger.error(
                "❌ add_memory(agent) %s: required conversation note was not created",
                source_id,
            )
            return False, result.touched
        if result.truncated or result.stalled:
            reason = (
                "truncated LLM response" if result.truncated else "stalled retry loop"
            )
            memory_logger.error(
                "❌ add_memory(agent) %s: source and partial mutations were preserved, "
                "but no agent completed deliberately (%s)",
                source_id,
                reason,
            )
            return False, result.touched
        memory_logger.info(
            "✅ add_memory(agent) %s: touched=%d rounds=%d tools=%d errors=%d (%.2fs) — %s",
            source_id,
            len(result.touched),
            result.rounds,
            result.tool_calls,
            len(result.errors),
            time.perf_counter() - t0,
            result.summary[:160],
        )
        return True, result.touched

    # =========================================================================
    # ADD DAY MEMORY
    # =========================================================================

    async def add_day_memory(
        self,
        day_digest: str,
        local_date: str,
        user_id: str,
        *,
        source_date: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Record one settled local day of timeline episodes into the vault.

        The conversation path (``add_memory``) is the wrong unit for capture evidence:
        ScreenPipe audio is cut into bounded compute spans, so one meeting can span
        several recordings. An episode already carries the right bounds, so the day of
        episodes — not the recordings under it — is what gets remembered.

        The record lands in ``Daily/<local_date>.md`` rather than under
        ``Conversations/``, which stays one note per conversation. Durable
        People/Topic/Category edits are unchanged.
        """
        write_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        recovery_backend = getattr(self.config, "write_recovery_backend", None)
        with memory_span(
            "memory_write_day",
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": local_date,
                "session.id": local_date,
                "langfuse.session.id": local_date,
                "chronicle.user_id": str(user_id),
                "langfuse.user.id": str(user_id),
                "chronicle.pipeline.stage": "memory_write",
                "chronicle.memory.operation": "write_day",
                "chronicle.memory.local_date": local_date,
                "chronicle.memory.primary_backend": write_backend,
                "chronicle.memory.recovery_backend": recovery_backend or "none",
                "chronicle.memory.transcript_chars": len(day_digest or ""),
            },
        ) as span:
            set_observation_io(
                span,
                input={
                    "local_date": local_date,
                    "transcript": text_payload(day_digest),
                },
            )
            await self._ensure_initialized()
            success, touched = await self._add_day_memory_agent(
                day_digest,
                local_date,
                user_id,
                source_date=source_date,
            )
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": success,
                    "chronicle.memory.touched_count": len(touched),
                },
            )
            set_observation_io(
                span,
                output={"success": success, "touched_count": len(touched)},
            )
            return success, touched

    async def _add_day_memory_agent(
        self,
        day_digest: str,
        local_date: str,
        user_id: str,
        *,
        source_date: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Write path for a settled day, with one recovery attempt.

        Deliberately simpler than ``_run_agent_with_note_guarantee``: that guarantee is
        built around a conversation note whose id the caller owns, and its
        source-preserving fallback writes a ``Conversations/`` note that would be wrong
        here. A day has no such deterministic fallback — an unwritten day stays
        unwritten, and the caller records it as failed so it can be retried.
        """
        # Lazy: ..agent imports llm_client, which imports this package's config back.
        from ..agent.memory_agent import (
            VERIFY_CAPABLE_BACKENDS,
            day_note_path,
            required_notes,
        )

        verify_expected = (
            self.config.write_agent_backend in VERIFY_CAPABLE_BACKENDS
            and (self.config.write_recovery_backend or "direct")
            in VERIFY_CAPABLE_BACKENDS
        )

        if not day_digest or len(day_digest.strip()) < 10:
            memory_logger.info("Skipping empty day digest for %s", local_date)
            return True, []

        t0 = time.perf_counter()
        trusted_date = source_date or datetime.now(timezone.utc).isoformat()
        user_root = self.vault.user_root(user_id)
        seed_vault_scaffold(user_root)  # idempotent: .base + hub notes
        existing_before = self._vault_note_set(user_root)
        day_rel = day_note_path(local_date)

        def day_note_written(agent_result) -> bool:
            """Did *this run* record the day note — not merely: does the file exist.

            Existence proves nothing here. Every date already has a ``Daily/<date>.md``
            from the retired per-observation curation, so an existence check passes for
            a run that edited some People notes and never wrote the day at all. Two of
            four backfilled days reported success that way before this was tightened.
            """

            return agent_result is not None and day_rel in (agent_result.touched or [])

        def deliberate_no_op(agent_result) -> bool:
            """Did the agent finish cleanly and decide nothing needed recording.

            A distinct outcome from failing to write. A day whose note already holds
            the episodes — every date carries 65-177 entries from the retired
            per-observation curation — is *correctly* left alone by an agent told not
            to duplicate what a note already has. Treating that as incomplete ran the
            recovery backend for nothing and then reported the day failed, so it was
            retried until it settled as skipped: two full agent runs per attempt to
            reach a conclusion the first run had already reached on purpose.
            """

            return (
                agent_result is not None
                and not agent_result.truncated
                and not agent_result.stalled
                and not agent_result.errors
                and bool((agent_result.summary or "").strip())
                and not agent_result.touched
                # A non-empty summary is not a conclusion. Both Qwen3.6 and DeepSeek V4
                # Pro end runs by narrating the next step as prose instead of emitting
                # the call — "Let me check the later parts of the day note for evening
                # episodes" — which reads as a clean finish with nothing to record. An
                # agent that genuinely decided the day needs nothing called verify_vault
                # first, as it is told to; one that stopped mid-thought did not.
                and (agent_result.verified or not verify_expected)
            )

        result = None
        for attempt, guidance in (
            ("primary", ""),
            (
                "recovery",
                "RECOVERY REQUIREMENT: the previous attempt did not finish recording "
                f"this day. You MUST write the day note at exactly "
                f"{day_note_path(local_date)}. Inspect what is already there and add "
                "only what is missing; do not duplicate facts already recorded.",
            ),
        ):
            agent_class = (
                self._write_agent_class()
                if attempt == "primary"
                else self._recovery_agent_class()
            )
            if agent_class is None:
                break
            with memory_attempt(attempt):
                try:
                    result = await agent_class(user_root).run(
                        day_digest,
                        local_date,
                        date=trusted_date,
                        guidance=guidance,
                        record="day",
                    )
                except Exception as exc:  # noqa: BLE001 - recovery/caller handles it
                    diagnostic = _safe_exception_diagnostic(exc)
                    memory_logger.error(
                        "Day memory %s backend failed for %s (%s)",
                        attempt,
                        local_date,
                        diagnostic,
                    )
                    result = None
                    continue
            if (day_note_written(result) or deliberate_no_op(result)) and not (
                result.truncated or result.stalled
            ):
                # A deliberate no-op ends the run here: there is nothing for the
                # recovery backend to recover.
                break

        # The agent is told to call verify_vault itself, but correctness must not depend
        # on it choosing to. Re-run the same checks here and give anything left back as
        # guidance for one repair pass — the findings name the note and the fix.
        #
        # The day note is required only of a run that edited something. An agent that
        # touched nothing judged the day already covered, which is a legitimate outcome;
        # one that wrote People notes and skipped the day note left the record itself
        # unwritten, which is the case worth repairing.
        def _required() -> tuple[str, ...]:
            return (
                required_notes("day", local_date)
                if result is not None and result.touched
                else ()
            )

        findings = verify_vault_changes(
            user_root, existing_before, required=_required()
        )
        repair_class = self._write_agent_class()
        if findings and result is not None and repair_class is not None:
            memory_logger.info(
                "🧹 Day %s: %d vault problem(s) left after the write; repairing",
                local_date,
                len(findings),
            )
            with memory_attempt("verify_repair"):
                try:
                    repair = await repair_class(user_root).run(
                        day_digest,
                        local_date,
                        date=trusted_date,
                        guidance=(
                            # "already recorded" is false when the finding IS that the
                            # day note is missing, and telling the agent not to add
                            # content would then forbid the very fix being asked for.
                            (
                                "REPAIR ONLY. Fix exactly these problems and nothing "
                                "else, then call verify_vault to confirm:\n"
                                if any(f.rule == "record_missing" for f in findings)
                                else "REPAIR ONLY. The day is already recorded; do not "
                                "add new content. Fix exactly these problems and "
                                "nothing else, then call verify_vault to confirm:\n"
                            )
                            + render_findings(findings)
                        ),
                        record="day",
                    )
                except Exception as exc:  # noqa: BLE001 - a failed repair is not fatal
                    memory_logger.warning(
                        "Day %s verify repair failed (%s)",
                        local_date,
                        _safe_exception_diagnostic(exc),
                    )
                else:
                    result.touched = list(
                        dict.fromkeys([*result.touched, *repair.touched])
                    )
            findings = verify_vault_changes(user_root, existing_before)
        if findings:
            # Not fatal: the day *is* recorded, and failing here would burn the day's
            # attempt budget and eventually skip it entirely over a schema nit. Loud
            # instead, so a rule the model cannot satisfy is visible rather than silent.
            memory_logger.warning(
                "🧹 Day %s recorded with %d unresolved vault problem(s): %s",
                local_date,
                len(findings),
                "; ".join(f"{f.path} [{f.rule}]" for f in findings),
            )

        touched = list(result.touched) if result else []
        await self._record_agent_touches(
            user_id,
            local_date,
            user_root,
            touched,
            existing_before,
            removed=result.removed if result else None,
        )
        if result is None:
            # Every backend raised. The note-exists check below cannot catch this: a
            # Daily note for this date may already exist from an earlier write, so its
            # presence says nothing about whether *this* run recorded anything. Without
            # this the day latches as `written` with an empty vault and is never retried.
            memory_logger.error(
                "❌ add_day_memory %s: no write backend completed (%.2fs)",
                local_date,
                time.perf_counter() - t0,
            )
            return False, touched
        if deliberate_no_op(result):
            memory_logger.info(
                "🗓️ add_day_memory %s: agent recorded nothing — it judged the day "
                "already covered (%.2fs): %s",
                local_date,
                time.perf_counter() - t0,
                (result.summary or "").strip()[:300],
            )
            return True, []
        if not day_note_written(result):
            memory_logger.error(
                "❌ add_day_memory %s: the day note %s was not written this run "
                "(touched %d other note(s)) (%.2fs)",
                local_date,
                day_rel,
                len(touched),
                time.perf_counter() - t0,
            )
            return False, touched
        if result is not None and (result.truncated or result.stalled):
            memory_logger.error(
                "❌ add_day_memory %s: partial mutations preserved, but no agent "
                "completed deliberately (%.2fs)",
                local_date,
                time.perf_counter() - t0,
            )
            return False, touched
        memory_logger.info(
            "✅ add_day_memory %s: touched=%d rounds=%d tools=%d errors=%d (%.2fs)",
            local_date,
            len(touched),
            result.rounds if result else 0,
            result.tool_calls if result else 0,
            len(result.errors) if result else 0,
            time.perf_counter() - t0,
        )
        return True, touched

    async def _run_agent_with_note_guarantee(
        self,
        agent_class,
        user_root: Path,
        transcript: str,
        source_id: str,
        *,
        guidance: str = "",
        source_date: Optional[str] = None,
        source_duration_minutes: Optional[float] = None,
        source_title: Optional[str] = None,
    ):
        """Recover when the primary fails or its conversation note is invalid."""
        trusted_date = source_date or datetime.now(timezone.utc).isoformat()
        before_primary = self._vault_note_set(user_root)
        with memory_attempt("primary"):
            try:
                # Resolve inside the guarantee so a runtime that disappears after
                # service initialization (binary/auth removed, invalidated mount, etc.)
                # still takes the explicitly configured recovery path.
                if agent_class is None:
                    agent_class = self._write_agent_class()
                result = await agent_class(user_root).run(
                    transcript,
                    source_id,
                    date=trusted_date,
                    duration_minutes=source_duration_minutes,
                    title=source_title,
                    guidance=guidance,
                )
            except Exception as exc:  # noqa: BLE001 - recovery backend handles it
                # Lazy import: circular dependency (agent → memory_agent →
                # llm_client → back into providers), same as _write_agent_class.
                from ..agent.memory_agent import MemoryAgentResult

                diagnostic = _safe_exception_diagnostic(exc)
                memory_logger.error(
                    "Primary memory write backend failed for %s (%s); trying the "
                    "configured recovery path",
                    source_id,
                    diagnostic,
                )
                after_primary = self._vault_note_set(user_root)
                touched = sorted(
                    path
                    for path, content in after_primary.items()
                    if before_primary.get(path) != content
                )
                removed = [
                    {"old_path": path, "new_path": "", "before": content}
                    for path, content in sorted(before_primary.items())
                    if path not in after_primary
                ]
                result = MemoryAgentResult(
                    conversation_id=source_id,
                    rounds=0,
                    touched=touched,
                    summary="",
                    removed=removed,
                    errors=[f"primary write backend failed ({diagnostic})"],
                    truncated=True,
                )
        note_name = Path(source_id).name
        expected_note = user_root / "Conversations" / f"{note_name}.md"
        primary_note_valid = self._canonicalize_conversation_note(
            expected_note,
            source_id,
            trusted_date,
            source_duration_minutes,
            source_title,
        )
        primary_incomplete = bool(result.truncated or result.stalled)
        if primary_note_valid and not primary_incomplete:
            result.touched = list(
                dict.fromkeys((*result.touched, f"Conversations/{note_name}.md"))
            )
            return result

        recovery_resolution_error = None
        with memory_attempt("recovery"):
            try:
                recovery_class = self._recovery_agent_class()
            except Exception as exc:  # noqa: BLE001 - deterministic fallback guaranteed
                recovery_class = None
                recovery_resolution_error = _safe_exception_diagnostic(exc)
                memory_logger.error(
                    "Memory recovery backend could not be resolved for %s (%s); "
                    "writing the source-preserving conversation note",
                    source_id,
                    recovery_resolution_error,
                )
        failure_description = (
            "stopped before deliberate completion"
            if primary_incomplete and primary_note_valid
            else f"did not create a valid Conversations/{note_name}.md note"
        )
        memory_logger.warning(
            "Memory write backend %s%s",
            failure_description,
            (
                "; trying the configured recovery backend"
                if recovery_class
                else "; using the source-preserving fallback path"
            ),
        )
        if primary_incomplete and primary_note_valid:
            recovery_requirement = (
                "RECOVERY REQUIREMENT: the previous attempt stopped before deliberate "
                "completion. Inspect the existing conversation and linked notes, then "
                "finish recording any transcript facts it missed. Do not duplicate facts."
            )
        else:
            recovery_requirement = (
                "RECOVERY REQUIREMENT: the previous attempt did not create the required "
                f"conversation note. You MUST write it at exactly "
                f"Conversations/{note_name}.md using conversation_id {source_id}. Do "
                "not alter or abbreviate the ID. The Summary and Key Facts sections "
                "MUST contain substantive text. For a short or low-information "
                "transcript, summarize the exact utterance rather than leaving either "
                "section blank."
            )
        recovery_guidance = (
            f"{guidance}\n\n" if guidance else ""
        ) + recovery_requirement
        # The recovery pass is best-effort: it may fail outright (no
        # defaults.fallback_llm configured, fallback unreachable, ...). The note
        # guarantee must survive that — degrade to an empty recovery result and
        # let the source-preserving fallback note below do its job, rather than
        # failing the whole memory job with nothing recorded.
        with memory_attempt("recovery"):
            if recovery_class is None:
                # Lazy: ..agent imports llm_client, which imports this package's config back.
                from ..agent.memory_agent import MemoryAgentResult

                recovery = MemoryAgentResult(
                    conversation_id=source_id,
                    rounds=0,
                    touched=[],
                    summary="",
                    errors=(
                        [f"recovery backend unavailable: {recovery_resolution_error}"]
                        if recovery_resolution_error is not None
                        else []
                    ),
                )
            else:
                try:
                    recovery = await recovery_class(
                        user_root,
                        # Reusing the direct runtime means retrying through its configured
                        # fallback model. Switching runtimes already supplies an independent
                        # recovery path, so use that runtime's primary model.
                        force_fallback=recovery_class is agent_class,
                    ).run(
                        transcript,
                        source_id,
                        date=trusted_date,
                        duration_minutes=source_duration_minutes,
                        title=source_title,
                        guidance=recovery_guidance,
                    )
                except Exception as exc:  # noqa: BLE001 - never lose the note
                    # Lazy import: circular dependency (agent → memory_agent →
                    # llm_client → providers), same as _write_agent_class above.
                    from ..agent.memory_agent import MemoryAgentResult

                    diagnostic = _safe_exception_diagnostic(exc)
                    memory_logger.error(
                        "Memory-agent recovery pass failed for %s (%s); falling back "
                        "to the source-preserving conversation note",
                        source_id,
                        diagnostic,
                    )
                    recovery = MemoryAgentResult(
                        conversation_id=source_id,
                        rounds=0,
                        touched=[],
                        summary="",
                        errors=[f"recovery pass failed ({diagnostic})"],
                        truncated=True,
                    )
        recovery.rounds += result.rounds
        recovery.tool_calls += result.tool_calls
        recovery.touched = list(dict.fromkeys((*result.touched, *recovery.touched)))
        recovery.removed = [*result.removed, *recovery.removed]
        recovery.errors = [*result.errors, *recovery.errors]
        if recovery_class is None:
            # No agent completed the interrupted work. The deterministic note path
            # preserves the source but must not make an incomplete primary look like
            # a deliberate semantic completion.
            recovery.truncated = recovery.truncated or result.truncated
            recovery.stalled = recovery.stalled or result.stalled
        for key, value in result.usage.items():
            recovery.usage[key] = recovery.usage.get(key, 0) + value
        recovery_valid = self._canonicalize_conversation_note(
            expected_note,
            source_id,
            trusted_date,
            source_duration_minutes,
            source_title,
        )
        recovery_incomplete = bool(recovery.truncated or recovery.stalled)
        if not recovery_valid or recovery_incomplete:
            memory_logger.warning(
                "Memory-agent attempts did not produce a complete valid note for %s; "
                "writing the deterministic source-preserving fallback",
                source_id,
            )
            with memory_attempt("fallback"):
                with memory_span(
                    "memory_write.source_fallback",
                    attributes={
                        "openinference.span.kind": "CHAIN",
                        "chronicle.memory.operation": "write_source_fallback",
                        "chronicle.memory.attempt": "fallback",
                        "gen_ai.conversation.id": source_id,
                    },
                ) as span:
                    set_observation_io(
                        span,
                        input={
                            "conversation_id": source_id,
                            "transcript": text_payload(transcript),
                            "title": text_payload(source_title),
                        },
                    )
                    write_source_fallback_conversation_note(
                        expected_note,
                        transcript=transcript,
                        conversation_id=source_id,
                        date=trusted_date,
                        duration_minutes=source_duration_minutes,
                        title=source_title,
                    )
                    set_safe_span_attributes(
                        span,
                        {
                            "chronicle.memory.success": True,
                            "chronicle.memory.touched_count": 1,
                        },
                    )
                    set_observation_io(
                        span,
                        output={"written": True, "touched_count": 1},
                    )
            recovery.touched = list(
                dict.fromkeys((*recovery.touched, f"Conversations/{note_name}.md"))
            )
        return recovery

    @staticmethod
    def _canonicalize_conversation_note(
        path: Path,
        source_id: str,
        source_date: str,
        source_duration_minutes: Optional[float],
        source_title: Optional[str],
    ) -> bool:
        if not path.is_file():
            return False
        try:
            canonicalize_conversation_note(
                path,
                conversation_id=source_id,
                date=source_date,
                duration_minutes=source_duration_minutes,
                title=source_title,
            )
            return True
        except ConversationNoteError as exc:
            memory_logger.warning("Invalid conversation note %s: %s", path, exc)
            path.unlink(missing_ok=True)
            return False

    async def _reprocess_memory_agent(
        self,
        transcript: str,
        source_id: str,
        user_id: str,
        transcript_diff: Optional[list],
    ) -> Tuple[bool, List[str]]:
        """Reprocess path.

        Deletes the stale conversation note so the agent re-records it cleanly, then runs
        the agent. When the reprocess came from speaker re-identification we hand the agent
        the old→new speaker map as guidance so it can ``rename_person`` (which rewrites all
        backlinks) instead of leaving orphaned ``[[Speaker 0]]`` notes. Person/topic notes
        are kept and surgically updated — only the conversation note is regenerated.
        """
        if not transcript or len(transcript.strip()) < 10:
            memory_logger.info(f"Skipping empty transcript for {source_id}")
            return True, []

        user_root = self.vault.user_root(user_id)
        guidance = self._speaker_rename_guidance(transcript_diff)

        t0 = time.perf_counter()
        # Per-user serialisation happens per-mutation inside VaultTools (see
        # _add_memory_agent).
        seed_vault_scaffold(user_root)
        existing_before = self._vault_note_set(user_root)

        # Remove the old conversation note (agent writes Conversations/<id>.md and
        # write_note refuses to clobber). Person/topic notes are preserved.
        conv_note = user_root / "Conversations" / f"{Path(source_id).name}.md"
        if conv_note.exists():
            conv_note.unlink()

        result = await self._run_agent_with_note_guarantee(
            None,
            user_root,
            transcript,
            source_id,
            guidance=guidance,
        )
        if result.truncated and not result.touched:
            memory_logger.error(
                "❌ reprocess_memory(agent) %s: aborted on truncated LLM response after "
                "%d rounds (%d tool calls) — nothing recorded (%.2fs)",
                source_id,
                result.rounds,
                result.tool_calls,
                time.perf_counter() - t0,
            )
            return False, []
        await self._record_agent_touches(
            user_id,
            source_id,
            user_root,
            result.touched,
            existing_before,
            removed=result.removed,
        )
        if not conv_note.is_file():
            memory_logger.error(
                "❌ reprocess_memory(agent) %s: required conversation note was not created",
                source_id,
            )
            return False, result.touched
        if result.truncated or result.stalled:
            reason = (
                "truncated LLM response" if result.truncated else "stalled retry loop"
            )
            memory_logger.error(
                "❌ reprocess_memory(agent) %s: source and partial mutations were "
                "preserved, but no agent completed deliberately (%s)",
                source_id,
                reason,
            )
            return False, result.touched
        memory_logger.info(
            "✅ reprocess_memory(agent) %s: touched=%d rounds=%d tools=%d errors=%d (%.2fs) — %s",
            source_id,
            len(result.touched),
            result.rounds,
            result.tool_calls,
            len(result.errors),
            time.perf_counter() - t0,
            result.summary[:160],
        )
        return True, result.touched

    def _vault_note_set(self, user_root: Path) -> dict[str, str]:
        """Snapshot vault-relative note contents for create/update audit records."""
        if not user_root.exists():
            return {}
        snapshot: dict[str, str] = {}
        for path in user_root.rglob("*.md"):
            try:
                snapshot[path.relative_to(user_root).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
            except OSError:
                continue
        return snapshot

    async def _record_agent_touches(
        self,
        user_id: str,
        source_id: str,
        user_root: Path,
        touched: Iterable[str],
        existing_before: dict[str, str],
        removed: Optional[Iterable[dict]] = None,
    ) -> None:
        """Record one audit-ledger entry per note the memory agent changed.

        ``removed`` are notes retired by a rename/merge (``VaultTools.removed``): each
        is logged as a ``rename`` entry carrying the pre-removal content, so a note
        disappearing from the vault is never invisible in the ledger (the gap that made
        a rename look like an unexplained clobber followed later by a fresh ``create``).
        """
        for entry in removed or ():
            await record_vault_change(
                user_id=user_id,
                conversation_id=source_id,
                operation="rename",
                note_path=entry.get("old_path"),
                before=entry.get("before"),
                after=None,
                agent_mode=True,
                summary=f"renamed/merged into {entry.get('new_path')}",
                new_path=entry.get("new_path"),
            )
        for rel in sorted(touched):
            try:
                after: Optional[str] = (user_root / rel).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — note may have been renamed away
                after = None
            is_new = rel not in existing_before
            await record_vault_change(
                user_id=user_id,
                conversation_id=source_id,
                operation="create" if is_new else "update",
                note_path=rel,
                before=existing_before.get(rel),
                after=after,
                agent_mode=True,
                summary=(
                    None
                    if is_new
                    else (
                        f"updated ({len(after.splitlines())} lines)"
                        if after is not None
                        else "updated"
                    )
                ),
            )

    # Diarization placeholders ("Speaker 0", "Unknown Speaker 1") — the only labels a
    # conversation-scoped diff may globally rename. A person note under a real name
    # aggregates facts from many conversations, so renaming it from one conversation's
    # relabel merges the wrong person's whole history (ankush.md -> roshan.md, 2026-07-17).
    _PLACEHOLDER_SPEAKER_RE = re.compile(
        r"^(unknown\s+)?speaker[\s_]*\d+$", re.IGNORECASE
    )

    @classmethod
    def _speaker_rename_guidance(cls, transcript_diff: Optional[list]) -> str:
        """Turn a speaker diff into an instruction to rename the matching person notes."""
        if not transcript_diff:
            return ""
        renames: dict[str, str] = {}
        relabels: dict[str, str] = {}
        for ch in transcript_diff:
            if isinstance(ch, dict) and ch.get("type") == "speaker_change":
                old, new = ch.get("old_speaker"), ch.get("new_speaker")
                if old and new and old != new:
                    if cls._PLACEHOLDER_SPEAKER_RE.match(old.strip()):
                        renames[old] = new
                    else:
                        relabels[old] = new
        parts: list[str] = []
        if renames:
            pairs = "; ".join(f"'{o}' is now '{n}'" for o, n in renames.items())
            parts.append(
                f"Placeholder speakers were identified: {pairs}. For each, if a "
                "People/<placeholder>.md note exists, call rename_person(old, new) FIRST "
                "— it renames the note and rewrites every [[wikilink]] across the vault — "
                "then record the conversation and update the renamed person notes. Do not "
                "leave notes under placeholder speaker labels."
            )
        if relabels:
            pairs = "; ".join(f"'{o}' is now '{n}'" for o, n in relabels.items())
            parts.append(
                f"Attribution changed between named people IN THIS CONVERSATION ONLY: "
                f"{pairs}. Fix the conversation note and move only facts sourced from "
                "this conversation between the affected person notes. Do NOT call "
                "rename_person for these — both notes describe real people whose facts "
                "come from many other conversations."
            )
        if not parts:
            return ""
        return "This is a REPROCESS after speaker re-identification. " + " ".join(parts)

    # =========================================================================
    # SEARCH
    # =========================================================================

    async def search_memories(
        self, query: str, user_id: str, limit: int = 10, score_threshold: float = 0.0
    ) -> List[MemoryEntry]:
        if not self._initialized:
            await self.initialize()
        return await self._search_vault_grep(query, user_id, limit)

    async def _search_vault_grep(
        self, query: str, user_id: str, limit: int
    ) -> List[MemoryEntry]:
        """Read path: a read-only retrieval agent drives ripgrep over the vault.

        Modelled on Claude Code — the LLM formulates ripgrep patterns (no query
        preprocessing), reads the relevant notes, and synthesises an answer. We return
        one MemoryEntry per note the agent read (capped), with the synthesised answer
        as the top entry so chat gets both the conclusion and the supporting notes.
        """
        # Lazy: ..agent imports llm_client, which imports this package's config back.
        from ..agent.memory_agent import is_search_failure_answer

        result, backend = await self._run_search_agent(query, user_id, limit)

        if result.errors:
            # Log the errors themselves, not just a count. A count tells you a
            # search degraded but not which tool call failed or why, which makes
            # a flaky retrieval agent undiagnosable from logs alone.
            memory_logger.warning(
                "Vault search backend=%s completed with %d error(s) (user: %s): %s",
                backend,
                len(result.errors),
                user_id,
                "; ".join(result.errors),
            )
        if result.warnings:
            memory_logger.info(
                "Vault search backend=%s completed with %d warning(s) (user: %s)",
                backend,
                len(result.warnings),
                user_id,
            )

        # Terminal sentinels are internal audit state, not user memory. Returning one
        # as a score-1 answer (or returning notes from an empty synthesis) contaminates
        # chat context with a failed search. Explicit evidence-based abstentions remain
        # ordinary nonempty answers and still flow through.
        if not result.answer.strip() or is_search_failure_answer(result.answer):
            memory_logger.warning(
                "Vault search backend=%s returned no usable answer (user: %s)",
                backend,
                user_id,
            )
            # Raise rather than return []. An empty list is indistinguishable from
            # a vault that genuinely holds nothing, and callers that assume the
            # latter go on to tell the user their vault is empty.
            raise VaultSearchUnavailable(
                f"The {backend} retrieval agent did not produce a usable answer"
                + (f" ({len(result.errors)} tool error(s))" if result.errors else "")
            )

        results: List[MemoryEntry] = []
        if result.answer:
            results.append(
                MemoryEntry(
                    id=f"search:{user_id}",
                    content=result.answer,
                    metadata={"user_id": user_id, "kind": "vault_search_answer"},
                    score=1.0,
                    created_at=None,
                )
            )
        for note in result.notes[: max(0, limit - len(results))]:
            path = note["path"]
            conv_id = ""
            if path.startswith("Conversations/"):
                conv_id = path.split("/", 1)[1].rsplit(".md", 1)[0]
            results.append(
                MemoryEntry(
                    id=path,
                    content=note["content"][:1500],
                    metadata={
                        "user_id": user_id,
                        "note": path,
                        "conversation_id": conv_id,
                        "kind": "vault_note",
                    },
                    score=0.9,
                    created_at=None,
                )
            )
        memory_logger.info(
            f"🔍 vault search: '{query}' -> {len(result.notes)} note(s) read, "
            f"{result.rounds} round(s), backend={backend} (user: {user_id})"
        )
        return results[:limit]

    async def _run_search_agent(self, query: str, user_id: str, limit: int):
        """Run and trace one configured retrieval backend without exposing note text."""
        # Lazy: ..agent imports llm_client, which imports this package's config back.
        from ..agent.memory_agent import is_search_failure_answer

        backend = (
            getattr(self.config, "search_agent_backend", "direct") or "direct"
        ).lower()
        with memory_span(
            "memory_search",
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.operation.name": "invoke_agent",
                "chronicle.pipeline.stage": "memory_search",
                "chronicle.memory.operation": "search",
                "chronicle.memory.backend": backend,
                "chronicle.memory.limit": limit,
                "chronicle.user_id": str(user_id),
                "langfuse.user.id": str(user_id),
                "chronicle.memory.query_chars": len(query),
            },
        ) as span:
            set_observation_io(
                span,
                input={"query": text_payload(query), "limit": limit},
            )
            if backend == "direct":
                # Lazy import: circular dependency (agent → memory_agent →
                # llm_client → services.memory.config → service_factory → here)
                from ..agent import search_vault

                result = await search_vault(
                    query,
                    self.vault.user_root(user_id),
                    operation="memory_search",
                )
            elif backend == "pi":
                # Lazy: ..agent imports llm_client, which imports this package's config back.
                from ..agent.pi_agent import search_vault_with_pi

                result = await search_vault_with_pi(
                    query,
                    self.vault.user_root(user_id),
                    operation="memory_search",
                )
            else:
                raise ValueError(f"Unsupported memory search backend: {backend}")

            usable_answer = bool(
                result.answer.strip()
            ) and not is_search_failure_answer(result.answer)
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": usable_answer and not result.truncated,
                    "chronicle.memory.usable_answer": usable_answer,
                    "chronicle.memory.rounds": result.rounds,
                    "chronicle.memory.tool_calls": result.tool_calls,
                    "chronicle.memory.notes_read_count": len(result.notes),
                    "chronicle.memory.error_count": len(result.errors),
                    "chronicle.memory.warning_count": len(result.warnings),
                    "chronicle.memory.final_synthesis_used": result.final_synthesis_used,
                    "chronicle.memory.truncated": result.truncated,
                    **{
                        f"chronicle.memory.usage.{key}": value
                        for key, value in result.usage.items()
                    },
                },
            )
            set_observation_io(
                span,
                output={
                    "answer": text_payload(result.answer),
                    "usable_answer": usable_answer,
                    "rounds": result.rounds,
                    "tool_calls": result.tool_calls,
                    "notes_read_count": len(result.notes),
                    "error_count": len(result.errors),
                    "warning_count": len(result.warnings),
                    "final_synthesis_used": result.final_synthesis_used,
                    "truncated": result.truncated,
                },
            )
            return result, backend

    # =========================================================================
    # CRUD
    # =========================================================================

    def _vault_entry_from_path(
        self, user_id: str, path: Path, root: Path, content_limit: Optional[int] = None
    ) -> Optional[MemoryEntry]:
        """Build a MemoryEntry from one vault note (id = vault-relative path)."""
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
        if content_limit is not None:
            content = content[:content_limit]
        rel = path.relative_to(root).as_posix()
        conv_id = (
            rel[len("Conversations/") : -3] if rel.startswith("Conversations/") else ""
        )
        return MemoryEntry(
            id=rel,
            content=content,
            metadata={
                "user_id": user_id,
                "note": rel,
                "conversation_id": conv_id,
                "kind": "vault_note",
            },
            created_at=None,
        )

    def _vault_entries(
        self, user_id: str, limit: Optional[int] = None
    ) -> List[MemoryEntry]:
        """Enumerate a user's vault notes as MemoryEntry objects (newest first).

        One entry per note, recursive over Conversations/People/Topics.
        """
        root = self.vault.user_root(user_id)
        if not root.exists():
            return []
        paths = sorted(
            (p for p in root.rglob("*.md") if not is_scaffold_note(p, root)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if limit is not None:
            paths = paths[:limit]
        entries: List[MemoryEntry] = []
        for p in paths:
            entry = self._vault_entry_from_path(user_id, p, root, content_limit=1500)
            if entry is not None:
                entries.append(entry)
        return entries

    async def get_all_memories(
        self, user_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        if not self._initialized:
            await self.initialize()
        return self._vault_entries(user_id, limit)

    async def count_memories(self, user_id: str) -> Optional[int]:
        if not self._initialized:
            await self.initialize()
        return len(self.vault.list_docs(user_id))

    async def get_memory(
        self, memory_id: str, user_id: Optional[str] = None
    ) -> Optional[MemoryEntry]:
        if not self._initialized:
            await self.initialize()

        if not user_id:
            memory_logger.error(
                "get_memory called without user_id; the vault is per-user"
            )
            return None

        # Memory ids are vault-relative note paths (see add_memory).
        root = self.vault.user_root(user_id)
        fp = root / memory_id
        if not fp.is_file():
            return None
        return self._vault_entry_from_path(user_id, fp, root)

    async def get_memories_by_source(
        self, user_id: str, source_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        """Return the conversation note for ``source_id`` (the vault's per-source record)."""
        if not self._initialized:
            await self.initialize()

        root = self.vault.user_root(user_id)
        conv_note = root / "Conversations" / f"{Path(source_id).name}.md"
        if not conv_note.is_file():
            return []
        entry = self._vault_entry_from_path(user_id, conv_note, root)
        return [entry] if entry is not None else []

    async def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        """Notes are edited by the memory agent (or by the user directly in the vault),
        not through this API."""
        memory_logger.warning(
            f"update_memory called for {memory_id} but vault notes are edited via the "
            "memory agent or directly in the vault. Use reprocess_memory to regenerate."
        )
        return False

    async def delete_memory(
        self,
        memory_id: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        if not self._initialized:
            await self.initialize()

        if not user_id:
            memory_logger.error(
                "delete_memory called without user_id; the vault is per-user"
            )
            return False

        # Memory ids are vault-relative note paths.
        root = self.vault.user_root(user_id)
        fp = root / memory_id
        try:
            if not fp.is_file():
                return False
            fp.unlink()
            await record_vault_change(
                user_id=user_id,
                operation="delete",
                note_path=Path(memory_id).as_posix(),
                agent_mode=False,
                summary=f"deleted {memory_id}",
            )
            memory_logger.info(f"🗑️ Deleted memory note {memory_id}")
            return True
        except Exception as e:
            memory_logger.error(f"Delete memory failed: {e}")
            return False

    async def delete_all_user_memories(self, user_id: str) -> int:
        if not self._initialized:
            await self.initialize()

        count = self.vault.delete_all_docs(user_id)  # vault is the only store
        await record_vault_change(
            user_id=user_id,
            operation="delete_all",
            agent_mode=False,
            summary=f"deleted {count} notes",
            count=count,
        )
        return count

    async def reprocess_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        transcript_diff: Optional[list] = None,
        previous_transcript: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Delete the stale conversation note and re-record from the transcript."""
        write_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        recovery_backend = getattr(self.config, "write_recovery_backend", None)
        with memory_span(
            "memory_write",
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": source_id,
                "session.id": source_id,
                "langfuse.session.id": source_id,
                "chronicle.user_id": str(user_id),
                "langfuse.user.id": str(user_id),
                "chronicle.client_id": client_id,
                "chronicle.pipeline.stage": "memory_write",
                "chronicle.memory.operation": "reprocess",
                "chronicle.memory.primary_backend": write_backend,
                "chronicle.memory.recovery_backend": recovery_backend or "none",
                "chronicle.memory.transcript_chars": len(transcript or ""),
                "chronicle.memory.reprocess": True,
                "chronicle.memory.transcript_diff_count": len(transcript_diff or []),
            },
        ) as span:
            set_observation_io(
                span,
                input={
                    "conversation_id": source_id,
                    "transcript": text_payload(transcript),
                    "previous_transcript": text_payload(previous_transcript),
                    "transcript_diff_count": len(transcript_diff or []),
                },
            )
            await self._ensure_initialized()
            success, touched = await self._reprocess_memory_agent(
                transcript, source_id, user_id, transcript_diff
            )
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": success,
                    "chronicle.memory.touched_count": len(touched),
                },
            )
            set_observation_io(
                span,
                output={"success": success, "touched_count": len(touched)},
            )
            return success, touched

    async def test_connection(self) -> bool:
        try:
            self._validate_configured_backends()
            return True
        except (RuntimeError, ValueError):
            return False

    def shutdown(self) -> None:
        self._initialized = False
        memory_logger.info("Memory service shut down")
