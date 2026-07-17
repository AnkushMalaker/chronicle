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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from ..audit import record_vault_change
from ..base import MemoryEntry, MemoryServiceBase
from ..config import MemoryConfig
from ..conversation_note import (
    ConversationNoteError,
    canonicalize_conversation_note,
    write_source_fallback_conversation_note,
)
from ..vault_manager import ConvDocVaultManager
from ..vault_scaffold import is_scaffold_note, seed_vault_scaffold

memory_logger = logging.getLogger("memory_service")


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
        # Vault-only: the agent generates + writes notes via its own tool-calling
        # LLM, and search greps the vault. No external index to connect to.
        self._initialized = True
        memory_logger.info("✅ Chronicle memory service initialized (agentic vault).")

    def _agent_class(self):
        """The write-agent executor: the built-in tool loop, or the Codex CLI.

        Falls back to the direct agent (with a warning) when Codex is configured but
        the CLI/auth is unavailable, so memory jobs keep flowing.
        """
        # Lazy import: circular dependency (agent → memory_agent → llm_client →
        # services.memory.config → service_factory → this module)
        from ..agent import MemoryAgent

        if (getattr(self.config, "agent_executor", "direct") or "direct") == "codex":
            from ..agent.codex_agent import CodexMemoryAgent, codex_executor_available

            available, detail = codex_executor_available()
            if available:
                return CodexMemoryAgent
            memory_logger.warning(
                "memory.agent_executor is 'codex' but the executor is unavailable "
                "(%s) — using the direct LLM agent",
                detail,
            )
        return MemoryAgent

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
        await self._ensure_initialized()
        return await self._add_memory_agent(
            transcript,
            source_id,
            user_id,
            source_date=source_date,
            source_duration_minutes=source_duration_minutes,
            source_title=source_title,
        )

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
            self._agent_class(),
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
        """Retry when the exact conversation note is absent or fails validation."""
        trusted_date = source_date or datetime.now(timezone.utc).isoformat()
        result = await agent_class(user_root).run(
            transcript,
            source_id,
            date=trusted_date,
            duration_minutes=source_duration_minutes,
            title=source_title,
            guidance=guidance,
        )
        note_name = Path(source_id).name
        expected_note = user_root / "Conversations" / f"{note_name}.md"
        if self._canonicalize_conversation_note(
            expected_note,
            source_id,
            trusted_date,
            source_duration_minutes,
            source_title,
        ):
            return result

        memory_logger.warning(
            "Memory agent did not create Conversations/%s.md; retrying with the "
            "configured fallback LLM",
            note_name,
        )
        recovery_guidance = (f"{guidance}\n\n" if guidance else "") + (
            "RECOVERY REQUIREMENT: the previous attempt did not create the required "
            f"conversation note. You MUST write it at exactly Conversations/{note_name}.md "
            f"using conversation_id {source_id}. Do not alter or abbreviate the ID. "
            "The Summary and Key Facts sections MUST contain substantive text. For a "
            "short or low-information transcript, summarize the exact utterance rather "
            "than leaving either section blank."
        )
        recovery = await agent_class(user_root, force_fallback=True).run(
            transcript,
            source_id,
            date=trusted_date,
            duration_minutes=source_duration_minutes,
            title=source_title,
            guidance=recovery_guidance,
        )
        recovery.rounds += result.rounds
        recovery.tool_calls += result.tool_calls
        recovery.touched = list(dict.fromkeys((*result.touched, *recovery.touched)))
        recovery.removed = [*result.removed, *recovery.removed]
        recovery.errors = [*result.errors, *recovery.errors]
        recovery_valid = self._canonicalize_conversation_note(
            expected_note,
            source_id,
            trusted_date,
            source_duration_minutes,
            source_title,
        )
        if not recovery_valid:
            memory_logger.warning(
                "Both memory-agent attempts produced an invalid note for %s; "
                "writing a source-preserving fallback",
                source_id,
            )
            write_source_fallback_conversation_note(
                expected_note,
                transcript=transcript,
                conversation_id=source_id,
                date=trusted_date,
                duration_minutes=source_duration_minutes,
                title=source_title,
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
            self._agent_class(),
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

    @staticmethod
    def _speaker_rename_guidance(transcript_diff: Optional[list]) -> str:
        """Turn a speaker diff into an instruction to rename the matching person notes."""
        if not transcript_diff:
            return ""
        renames: dict[str, str] = {}
        for ch in transcript_diff:
            if isinstance(ch, dict) and ch.get("type") == "speaker_change":
                old, new = ch.get("old_speaker"), ch.get("new_speaker")
                if old and new and old != new:
                    renames[old] = new
        if not renames:
            return ""
        pairs = "; ".join(f"'{o}' is now '{n}'" for o, n in renames.items())
        return (
            "This is a REPROCESS after speaker re-identification. Speaker labels changed: "
            f"{pairs}. For each change, if a People/<old name>.md note exists, call "
            "rename_person(old, new) FIRST — it renames the note and rewrites every "
            "[[wikilink]] across the vault — then record the conversation and update the "
            "renamed person notes. Do not leave notes under the old speaker labels."
        )

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
        # Lazy import: circular dependency (agent → memory_agent → llm_client →
        # services.memory.config → service_factory → this module)
        from ..agent import search_vault

        result = await search_vault(query, self.vault.user_root(user_id))

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
            f"{result.rounds} round(s) (user: {user_id})"
        )
        return results[:limit]

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
        await self._ensure_initialized()
        return await self._reprocess_memory_agent(
            transcript, source_id, user_id, transcript_diff
        )

    async def test_connection(self) -> bool:
        return True  # vault-only: nothing external to probe

    def shutdown(self) -> None:
        self._initialized = False
        memory_logger.info("Memory service shut down")
