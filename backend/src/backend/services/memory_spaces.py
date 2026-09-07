"""Lifecycle, seeding, review, and publication for isolated memory spaces."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

from beanie.operators import In
from pymongo import ReturnDocument

from backend.models.audio_capture import AudioCaptureSession
from backend.models.conversation import Conversation
from backend.models.memory_audit import MemoryAuditEntry
from backend.models.memory_space import (
    DeferredSpaceEvent,
    MemorySpace,
    SeededVaultNote,
    SpaceMergeChange,
    SpaceMergeProposal,
    SpaceSourceRef,
    SpaceValidationFinding,
)
from backend.plugins.events import PluginEvent
from backend.services.memory.audit import record_vault_change
from backend.services.memory.scope import (
    MemoryScope,
    MemoryScopeError,
    MemoryScopeResolver,
)
from backend.services.memory.vault_lock import vault_run_lock
from backend.services.memory.vault_scaffold import (
    confined_vault_path,
    is_scaffold_note,
    safe_vault_relative_path,
    seed_vault_scaffold,
)
from backend.services.memory.vault_verify import verify_vault_changes
from backend.services.plugin_service import dispatch_plugin_event
from backend.services.timeline.dirty_ranges import note_conversation_dirty
from backend.services.vault_sync import vault_sync_broker

_WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_MARKDOWN_MEDIA_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


class MemorySpaceConflict(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(text: Optional[str]) -> Optional[str]:
    return (
        hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None
    )


def _snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.md"))
        if path.is_file() and not is_scaffold_note(path, root)
    }


def _validation_snapshot(root: Path) -> dict[str, str]:
    """Snapshot every Markdown file that will be present in staged Main."""

    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.md"))
        if path.is_file()
    }


def _replace_tree(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    if source.exists():
        shutil.copytree(source, temporary)
    else:
        temporary.mkdir(parents=True)
    if target.exists():
        shutil.rmtree(target)
    os.replace(temporary, target)


def _write_note(root: Path, note_path: str, text: Optional[str]) -> None:
    target = confined_vault_path(root, note_path)
    if text is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)


def _split_markdown_sections(
    text: str,
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split a note while preserving its exact lines and section order."""

    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    current_heading = ""
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections.append((current_heading, current))
            current_heading = line
            current = [line]
        elif current is None:
            preamble.append(line)
        else:
            current.append(line)
    if current is not None:
        sections.append((current_heading, current))
    return preamble, sections


def _merge_created_note(main_text: str, workspace_text: str) -> str:
    """Merge a note created independently in a blank space into existing Main.

    Main's frontmatter, title, prose, and ordering remain authoritative. Workspace
    facts are added to matching Markdown sections, and entirely new sections are
    appended. This produces a reviewable candidate without replacing Main wholesale.
    The semantic reviewer still checks the added lines against the source and vault.
    """

    main_preamble, main_sections = _split_markdown_sections(main_text)
    space_preamble, space_sections = _split_markdown_sections(workspace_text)
    main_lines = set(main_text.splitlines())

    # A separately-created note commonly repeats YAML and its H1. Those are schema,
    # not workspace facts. Preserve Main's exact versions and only add novel prose.
    in_frontmatter = False
    preamble_additions: list[str] = []
    for line in space_preamble:
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter or line.startswith("# ") or not line.strip():
            continue
        if line not in main_lines:
            preamble_additions.append(line)

    section_index = {
        heading.casefold(): index for index, (heading, _) in enumerate(main_sections)
    }
    merged_sections = [(heading, list(lines)) for heading, lines in main_sections]
    for heading, workspace_lines in space_sections:
        index = section_index.get(heading.casefold())
        if index is None:
            merged_sections.append((heading, list(workspace_lines)))
            section_index[heading.casefold()] = len(merged_sections) - 1
            continue
        _, target_lines = merged_sections[index]
        known = {line for line in target_lines if line.strip()}
        additions = [
            line for line in workspace_lines[1:] if line.strip() and line not in known
        ]
        if additions:
            while target_lines and not target_lines[-1].strip():
                target_lines.pop()
            target_lines.extend(["", *additions])

    merged: list[str] = list(main_preamble)
    if preamble_additions:
        while merged and not merged[-1].strip():
            merged.pop()
        merged.extend(["", *preamble_additions])
    for _, section_lines in merged_sections:
        while merged and not merged[-1].strip():
            merged.pop()
        merged.extend(["", *section_lines])
    return "\n".join(merged).rstrip() + "\n"


class MemorySpaceService:
    def __init__(self, resolver: Optional[MemoryScopeResolver] = None):
        self.resolver = resolver or MemoryScopeResolver()

    async def get(self, user_id: str, space_id: str) -> MemorySpace:
        return await self.resolver.require_space(MemoryScope(user_id, space_id))

    async def list(self, user_id: str) -> list[MemorySpace]:
        return (
            await MemorySpace.find(MemorySpace.user_id == str(user_id))
            .sort("-updated_at")
            .to_list()
        )

    async def preview_seed(self, user_id: str, note_paths: Iterable[str]) -> dict:
        main_root = self.resolver.main_root(user_id)
        selected: list[dict] = []
        suggestions: dict[str, dict] = {}
        total_bytes = 0
        for raw in dict.fromkeys(note_paths):
            rel = safe_vault_relative_path(raw)
            if not rel.endswith(".md"):
                raise MemoryScopeError(f"Seed note must be Markdown: {rel}")
            path = confined_vault_path(main_root, rel)
            if not path.is_file() or is_scaffold_note(path, main_root):
                raise MemoryScopeError(f"Main note not found: {rel}")
            text = path.read_text(encoding="utf-8")
            size = len(text.encode("utf-8"))
            selected.append({"note_path": rel, "byte_size": size})
            total_bytes += size
            for link in _WIKILINK_RE.findall(text):
                candidate = link if link.endswith(".md") else f"{link}.md"
                try:
                    linked = confined_vault_path(main_root, candidate)
                except Exception:
                    continue
                if linked.is_file() and not is_scaffold_note(linked, main_root):
                    linked_rel = linked.relative_to(main_root).as_posix()
                    if linked_rel not in {item["note_path"] for item in selected}:
                        suggestions[linked_rel] = {
                            "note_path": linked_rel,
                            "byte_size": linked.stat().st_size,
                        }
        return {
            "selected": selected,
            "suggestions": sorted(
                suggestions.values(), key=lambda item: item["note_path"]
            ),
            "total_bytes": total_bytes,
        }

    async def search_main_notes(
        self, user_id: str, query: str = "", limit: int = 100
    ) -> list[dict]:
        root = self.resolver.main_root(user_id)
        if not root.exists():
            return []
        needle = query.strip().casefold()
        results: list[dict] = []
        for path in sorted(root.rglob("*.md")):
            if not path.is_file() or is_scaffold_note(path, root):
                continue
            rel = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            if (
                needle
                and needle not in rel.casefold()
                and needle not in text.casefold()
            ):
                continue
            excerpt = " ".join(text.split())[:240]
            results.append(
                {
                    "note_path": rel,
                    "byte_size": len(text.encode("utf-8")),
                    "excerpt": excerpt,
                }
            )
            if len(results) >= max(1, min(limit, 250)):
                break
        return results

    def _copy_seed_sync(
        self, scope: MemoryScope, note_paths: Iterable[str]
    ) -> list[SeededVaultNote]:
        main_root = self.resolver.main_root(scope.user_id)
        vault_root = self.resolver.vault_root(scope)
        vault_root.mkdir(parents=True, exist_ok=True)
        seed_vault_scaffold(vault_root)
        copied: list[SeededVaultNote] = []
        for raw in dict.fromkeys(note_paths):
            rel = safe_vault_relative_path(raw)
            source = confined_vault_path(main_root, rel)
            if not source.is_file() or is_scaffold_note(source, main_root):
                raise MemoryScopeError(f"Main note not found: {rel}")
            text = source.read_text(encoding="utf-8")
            _write_note(vault_root, rel, text)
            copied.append(
                SeededVaultNote(
                    note_path=rel,
                    content_hash=_hash(text) or "",
                    byte_size=len(text.encode("utf-8")),
                )
            )
            media_refs = list(_WIKILINK_RE.findall(text)) + list(
                _MARKDOWN_MEDIA_RE.findall(text)
            )
            for media_ref in media_refs:
                if not media_ref.startswith("_media/"):
                    continue
                try:
                    media_source = confined_vault_path(main_root, media_ref)
                    media_target = confined_vault_path(vault_root, media_ref)
                except Exception:
                    continue
                if media_source.is_file():
                    media_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(media_source, media_target)
        _replace_tree(vault_root, self.resolver.baseline_root(scope))
        return copied

    async def create(
        self, user_id: str, name: str, note_paths: Iterable[str] = ()
    ) -> MemorySpace:
        clean_name = name.strip()
        if not clean_name:
            raise MemoryScopeError("Memory space name is required")
        space = MemorySpace(user_id=str(user_id), name=clean_name)
        await space.insert()
        scope = MemoryScope(str(user_id), space.space_id)
        try:
            space.seed_notes = await asyncio.to_thread(
                self._copy_seed_sync, scope, list(note_paths)
            )
            space.merge_checkpoint = self.snapshot_hash(
                _snapshot(self.resolver.vault_root(scope))
            )
            await space.save()
            return space
        except Exception:
            await space.delete()
            base = self.resolver.space_base(scope)
            if base.exists():
                await asyncio.to_thread(shutil.rmtree, base)
            raise

    async def rename(self, user_id: str, space_id: str, name: str) -> MemorySpace:
        space = await self.get(user_id, space_id)
        clean_name = name.strip()
        if not clean_name:
            raise MemoryScopeError("Memory space name is required")
        space.name = clean_name
        space.updated_at = _utcnow()
        await space.save()
        return space

    async def reopen(self, user_id: str, space_id: str) -> MemorySpace:
        space = await self.get(user_id, space_id)
        if space.state != "archived":
            raise MemoryScopeError("Only archived spaces can be reopened")
        scope = MemoryScope(user_id, space_id)
        next_sync_state = space.sync_state
        if space.sync_state == "frozen":
            await vault_sync_broker.set_frozen(scope, False, space_name=space.name)
            next_sync_state = "syncing"
        await asyncio.to_thread(
            _replace_tree,
            self.resolver.vault_root(scope),
            self.resolver.baseline_root(scope),
        )
        space.state = "active"
        space.active_merge_proposal_id = None
        space.archived_at = None
        space.sync_state = next_sync_state
        space.updated_at = _utcnow()
        space.merge_checkpoint = self.snapshot_hash(
            _snapshot(self.resolver.vault_root(scope))
        )
        await space.save()
        return space

    async def notes(self, user_id: str, space_id: str) -> list[dict]:
        scope = MemoryScope(user_id, space_id)
        await self.resolver.require_space(scope)
        root = self.resolver.vault_root(scope)
        return [
            {
                "note_path": path.relative_to(root).as_posix(),
                "content": path.read_text(encoding="utf-8"),
                "updated_at": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ),
            }
            for path in sorted(root.rglob("*.md"))
            if path.is_file() and not is_scaffold_note(path, root)
        ]

    @staticmethod
    def snapshot_hash(snapshot: Mapping[str, str]) -> str:
        digest = hashlib.sha256()
        for path, text in sorted(snapshot.items()):
            digest.update(path.encode())
            digest.update(b"\0")
            digest.update(text.encode())
            digest.update(b"\0")
        return digest.hexdigest()

    async def _source_refs(self, user_id: str, space_id: str, note_path: str):
        rows = await MemoryAuditEntry.find(
            MemoryAuditEntry.user_id == user_id,
            MemoryAuditEntry.memory_space_id == space_id,
            MemoryAuditEntry.note_path == note_path,
        ).to_list()
        refs: dict[tuple[str, str], SpaceSourceRef] = {}
        for row in rows:
            source_id = row.conversation_id or str(row.extra.get("source_id") or "")
            if not source_id:
                continue
            source_type = str(row.extra.get("source_type") or "")
            if source_id.startswith("chat_") or source_type == "chat":
                kind = "chat"
            elif source_type == "manual":
                kind = "manual"
            elif source_type in {"obsidian_sync", "obsidian_action"}:
                kind = "obsidian"
            else:
                kind = "conversation"
            refs[(kind, source_id)] = SpaceSourceRef(kind=kind, source_id=source_id)
        return list(refs.values())

    async def _await_processing_drain(self, user_id: str, space_id: str) -> None:
        timeout = float(os.getenv("MEMORY_SPACE_MERGE_DRAIN_TIMEOUT_SECONDS", "30"))
        deadline = asyncio.get_running_loop().time() + max(timeout, 0)
        consecutive_quiet_polls = 0
        while True:
            active_captures = await AudioCaptureSession.find(
                AudioCaptureSession.user_id == user_id,
                AudioCaptureSession.memory_space_id == space_id,
                AudioCaptureSession.status == "active",
            ).count()
            active_conversations = await Conversation.find(
                Conversation.user_id == user_id,
                Conversation.memory_space_id == space_id,
                {
                    "processing_status": {
                        "$in": [
                            Conversation.ConversationStatus.ACTIVE.value,
                            None,
                        ]
                    }
                },
            ).count()
            if not active_captures and not active_conversations:
                # Capture admission validates the space before initializing its
                # durable session. Requiring two quiet observations closes the
                # small interval in which an already-admitted start can still be
                # inserting that session while the space changes to `merging`.
                consecutive_quiet_polls += 1
                if timeout <= 0 or consecutive_quiet_polls >= 2:
                    return
            else:
                consecutive_quiet_polls = 0
            if asyncio.get_running_loop().time() >= deadline:
                raise MemorySpaceConflict(
                    "Space still has active capture or processing; stop recording and retry"
                )
            await asyncio.sleep(0.25)

    async def _review_sources(self, changes: list[SpaceMergeChange]) -> str:
        conversation_ids = sorted(
            {
                ref.source_id
                for change in changes
                for ref in change.source_refs
                if ref.kind == "conversation"
            }
        )
        parts: list[str] = []
        if conversation_ids:
            conversations = await Conversation.find(
                {"conversation_id": {"$in": conversation_ids}}
            ).to_list()
            parts.extend(
                f"Conversation {item.conversation_id}:\n{item.transcript or ''}"
                for item in conversations
            )
        chat_ids = sorted(
            {
                ref.source_id.removeprefix("chat_")
                for change in changes
                for ref in change.source_refs
                if ref.kind == "chat"
            }
        )
        if chat_ids:
            # Lazy import avoids binding the application database during service import.
            from backend.database import get_database

            cursor = (
                get_database()["chat_messages"]
                .find({"session_id": {"$in": chat_ids}})
                .sort([("session_id", 1), ("timestamp", 1)])
            )
            async for message in cursor:
                parts.append(
                    f"Chat {message.get('session_id')} {message.get('role')}: "
                    f"{message.get('content', '')}"
                )
        return "\n\n".join(parts)

    async def prepare_merge(
        self,
        user_id: str,
        space_id: str,
        *,
        acknowledge_sync_warnings: bool = False,
    ) -> SpaceMergeProposal:
        space = await self.get(user_id, space_id)
        if space.state != "active":
            raise MemoryScopeError("Only an active space can prepare a merge")
        proposal = SpaceMergeProposal(user_id=user_id, space_id=space_id)
        now = _utcnow()
        claimed = await MemorySpace.get_pymongo_collection().update_one(
            {
                "user_id": user_id,
                "space_id": space_id,
                "state": "active",
            },
            {
                "$set": {
                    "state": "merging",
                    "active_merge_proposal_id": proposal.proposal_id,
                    "updated_at": now,
                }
            },
        )
        if claimed.modified_count != 1:
            raise MemorySpaceConflict("Memory space is already preparing a merge")
        space.state = "merging"
        space.active_merge_proposal_id = proposal.proposal_id
        space.updated_at = now
        scope = MemoryScope(user_id, space_id)
        try:
            if space.sync_state != "unpaired":
                await vault_sync_broker.rescan(scope, space_name=space.name)
                sync_health = await vault_sync_broker.health(
                    scope, space_name=space.name
                )
                if not sync_health.get("healthy"):
                    raise MemorySpaceConflict(
                        "Paired vault is not healthy; resolve sync before merging"
                    )
                if sync_health.get("warnings") and not acknowledge_sync_warnings:
                    raise MemorySpaceConflict(
                        "Paired device is offline or stale; acknowledge sync warnings to continue"
                    )
            await self._await_processing_drain(user_id, space_id)
            baseline = await asyncio.to_thread(
                _snapshot, self.resolver.baseline_root(scope)
            )
            workspace = await asyncio.to_thread(
                _snapshot, self.resolver.vault_root(scope)
            )
            main_root = self.resolver.main_root(user_id)
            main_before = await asyncio.to_thread(_snapshot, main_root)
            changes: list[SpaceMergeChange] = []
            for note_path in sorted(set(baseline) | set(workspace)):
                base_text = baseline.get(note_path)
                space_text = workspace.get(note_path)
                if base_text == space_text:
                    continue
                main_text = main_before.get(note_path)
                conflict = None
                after_text = space_text
                if (
                    base_text is None
                    and main_text is not None
                    and space_text is not None
                ):
                    after_text = _merge_created_note(main_text, space_text)
                elif main_text != base_text and main_text != space_text:
                    conflict = "Main changed independently; resolve this note before publication"
                operation = (
                    "delete"
                    if space_text is None
                    else "create" if main_text is None else "update"
                )
                changes.append(
                    SpaceMergeChange(
                        note_path=note_path,
                        operation=operation,
                        before_hash=_hash(main_text),
                        before_text=main_text,
                        after_text=after_text,
                        conflict=conflict,
                        validation_findings=(
                            [
                                SpaceValidationFinding(
                                    rule="main_changed",
                                    detail=conflict,
                                    severity="conflict",
                                )
                            ]
                            if conflict
                            else []
                        ),
                        source_refs=await self._source_refs(
                            user_id, space_id, note_path
                        ),
                    )
                )

            with tempfile.TemporaryDirectory(prefix="chronicle-space-merge-") as tmp:
                stage = Path(tmp) / "vault"
                if main_root.exists():
                    await asyncio.to_thread(shutil.copytree, main_root, stage)
                else:
                    stage.mkdir(parents=True)
                    seed_vault_scaffold(stage)
                validation_before = await asyncio.to_thread(_validation_snapshot, stage)
                for change in changes:
                    if change.conflict is None:
                        _write_note(stage, change.note_path, change.after_text)
                findings = verify_vault_changes(stage, validation_before)
                if findings:
                    rendered = "; ".join(
                        f"{finding.path} [{finding.rule}] {finding.detail}"
                        for finding in findings
                    )
                    raise MemorySpaceConflict(
                        f"Staged Main vault failed validation: {rendered}"
                    )
                review_source = await self._review_sources(changes)
                if review_source:
                    # Lazy import keeps the heavyweight semantic reviewer off basic space operations.
                    from backend.services.memory.agent.review_agent import (
                        review_vault_write,
                    )

                    review = await review_vault_write(
                        stage,
                        source=review_source,
                        before=validation_before,
                        touched=[
                            change.note_path
                            for change in changes
                            if change.conflict is None
                        ],
                        record=f"memory-space:{space_id}",
                        operation="memory_space_merge_review",
                    )
                    by_path = {change.note_path: change for change in changes}
                    for finding in review.findings:
                        change = by_path.get(finding.path)
                        if change is None:
                            continue
                        detail = str(getattr(finding, "detail", finding.rule))
                        change.validation_findings.append(
                            SpaceValidationFinding(
                                rule=finding.rule,
                                detail=detail,
                                severity="semantic",
                            )
                        )
                        change.conflict = f"{finding.rule}: {detail}"

            proposal.changes = changes
            proposal.deferred_event_count = await DeferredSpaceEvent.find(
                DeferredSpaceEvent.user_id == user_id,
                DeferredSpaceEvent.space_id == space_id,
                DeferredSpaceEvent.state == "pending",
            ).count()
            proposal.state = "pending"
            proposal.generated_at = _utcnow()
            await proposal.insert()
            return proposal
        except Exception:
            await MemorySpace.get_pymongo_collection().update_one(
                {
                    "user_id": user_id,
                    "space_id": space_id,
                    "state": "merging",
                    "active_merge_proposal_id": proposal.proposal_id,
                },
                {
                    "$set": {"state": "active", "updated_at": _utcnow()},
                    "$unset": {"active_merge_proposal_id": ""},
                },
            )
            raise

    async def resolve_merge(
        self, user_id: str, proposal_id: str, accepted_change_ids: Iterable[str]
    ) -> SpaceMergeProposal:
        proposal = await SpaceMergeProposal.find_one(
            SpaceMergeProposal.proposal_id == proposal_id,
            SpaceMergeProposal.user_id == user_id,
        )
        if proposal is None or proposal.state != "pending":
            raise MemorySpaceConflict("Merge proposal is not pending")
        accepted = set(accepted_change_ids)
        known = {change.change_id for change in proposal.changes}
        if not accepted.issubset(known):
            raise MemorySpaceConflict("Unknown merge change selected")
        selected = [
            change for change in proposal.changes if change.change_id in accepted
        ]
        if any(change.conflict for change in selected):
            raise MemorySpaceConflict("Conflicted changes cannot be applied")
        claimed = await SpaceMergeProposal.get_pymongo_collection().update_one(
            {
                "proposal_id": proposal_id,
                "user_id": user_id,
                "state": "pending",
            },
            {"$set": {"state": "applying", "error": None}},
        )
        if claimed.modified_count != 1:
            raise MemorySpaceConflict("Merge proposal is already being resolved")
        proposal.state = "applying"
        proposal.error = None
        space = await self.get(user_id, proposal.space_id)
        if (
            space.state != "merging"
            or space.active_merge_proposal_id != proposal.proposal_id
        ):
            proposal.state = "stale"
            proposal.error = "Memory space changed before the proposal was resolved"
            await proposal.save()
            raise MemorySpaceConflict(proposal.error)
        main_root = self.resolver.main_root(user_id)
        main_root.mkdir(parents=True, exist_ok=True)
        applied: list[tuple[SpaceMergeChange, Optional[str]]] = []
        try:
            with vault_run_lock(user_id):
                for change in selected:
                    target = confined_vault_path(main_root, change.note_path)
                    current = (
                        target.read_text(encoding="utf-8") if target.is_file() else None
                    )
                    if _hash(current) != change.before_hash:
                        raise MemorySpaceConflict(
                            f"{change.note_path} changed after the proposal was generated"
                        )
                try:
                    for change in selected:
                        target = confined_vault_path(main_root, change.note_path)
                        before = (
                            target.read_text(encoding="utf-8")
                            if target.is_file()
                            else None
                        )
                        _write_note(main_root, change.note_path, change.after_text)
                        applied.append((change, before))
                except Exception:
                    for applied_change, before in reversed(applied):
                        _write_note(main_root, applied_change.note_path, before)
                    raise
        except Exception as exc:
            proposal.state = "stale"
            proposal.error = str(exc)
            await proposal.save()
            raise

        source_refs = {
            (ref.kind, ref.source_id)
            for change in selected
            for ref in change.source_refs
        }
        source_ids = {
            source_id for kind, source_id in source_refs if kind == "conversation"
        }
        now = _utcnow()
        if source_ids:
            await Conversation.get_pymongo_collection().update_many(
                {
                    "user_id": user_id,
                    "memory_space_id": proposal.space_id,
                    "conversation_id": {"$in": sorted(source_ids)},
                },
                {
                    "$set": {
                        "published_to_main_at": now,
                        "published_by_merge_proposal_id": proposal.proposal_id,
                    }
                },
            )
        for change, before in applied:
            await record_vault_change(
                user_id=user_id,
                operation=change.operation,
                note_path=change.note_path,
                before=before,
                after=change.after_text,
                summary=f"published from memory space {proposal.space_id}",
                space_merge_proposal_id=proposal.proposal_id,
            )
        for source_id in source_ids:
            await note_conversation_dirty(source_id, "memory_space_published")
        await self._dispatch_deferred(proposal, source_refs)

        proposal.accepted_change_ids = sorted(accepted)
        proposal.rejected_change_ids = sorted(known - accepted)
        proposal.state = "applied"
        proposal.resolved_at = now
        await proposal.save()
        scope = MemoryScope(user_id, proposal.space_id)
        had_sync_folder = space.sync_state != "unpaired"
        await asyncio.to_thread(
            _replace_tree,
            self.resolver.vault_root(scope),
            self.resolver.baseline_root(scope),
        )
        space.state = "archived"
        space.active_merge_proposal_id = None
        if had_sync_folder:
            try:
                await vault_sync_broker.set_frozen(scope, True, space_name=space.name)
                space.sync_state = "frozen"
                space.sync_error = None
            except Exception as exc:
                # Publication is already committed. A sync failure is separately
                # retryable and must never roll Main back.
                space.sync_state = "error"
                space.sync_error = f"{type(exc).__name__}: {exc}"[:1000]
        space.archived_at = now
        space.updated_at = now
        space.merge_checkpoint = self.snapshot_hash(
            _snapshot(self.resolver.vault_root(scope))
        )
        await space.save()
        return proposal

    async def latest_merge_proposal(
        self, user_id: str, space_id: str
    ) -> Optional[SpaceMergeProposal]:
        """Return only the proposal associated with the space's current cycle."""

        space = await self.get(user_id, space_id)
        if space.state == "active":
            return None
        filters = [
            SpaceMergeProposal.user_id == user_id,
            SpaceMergeProposal.space_id == space_id,
        ]
        if space.state == "merging":
            if not space.active_merge_proposal_id:
                return None
            filters.append(
                SpaceMergeProposal.proposal_id == space.active_merge_proposal_id
            )
        else:
            filters.append(SpaceMergeProposal.state == "applied")
        proposals = (
            await SpaceMergeProposal.find(*filters)
            .sort([("created_at", -1)])
            .limit(1)
            .to_list()
        )
        return proposals[0] if proposals else None

    async def cancel_merge(self, user_id: str, proposal_id: str) -> SpaceMergeProposal:
        """Discard a proposal and reopen its workspace without publishing Main."""
        now = _utcnow()
        document = (
            await SpaceMergeProposal.get_pymongo_collection().find_one_and_update(
                {
                    "proposal_id": proposal_id,
                    "user_id": user_id,
                    "state": {"$in": ["pending", "stale", "failed"]},
                },
                {
                    "$set": {
                        "state": "cancelled",
                        "error": "Returned to editing before publication",
                        "resolved_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is None:
            proposal = await SpaceMergeProposal.find_one(
                SpaceMergeProposal.proposal_id == proposal_id,
                SpaceMergeProposal.user_id == user_id,
            )
            if proposal is None or proposal.state != "cancelled":
                raise MemorySpaceConflict("Merge proposal cannot return to editing")
        else:
            proposal = SpaceMergeProposal.model_validate(document)
        space = await self.get(user_id, proposal.space_id)
        if space.state == "merging":
            result = await MemorySpace.get_pymongo_collection().update_one(
                {
                    "user_id": user_id,
                    "space_id": proposal.space_id,
                    "state": "merging",
                    "active_merge_proposal_id": proposal.proposal_id,
                },
                {
                    "$set": {"state": "active", "updated_at": now},
                    "$unset": {"active_merge_proposal_id": ""},
                },
            )
            if result.modified_count == 1:
                return proposal
            space = await self.get(user_id, proposal.space_id)
        if space.state != "active" or space.active_merge_proposal_id is not None:
            raise MemorySpaceConflict("Memory space is not waiting for merge review")
        # A retry repairs the only cross-document partial outcome: a cancelled
        # proposal whose workspace update was not observed by the first caller.
        return proposal

    async def _dispatch_deferred(
        self, proposal: SpaceMergeProposal, source_refs: set[tuple[str, str]]
    ) -> None:
        if not source_refs:
            return
        source_ids = sorted({source_id for _, source_id in source_refs})
        events = (
            await DeferredSpaceEvent.find(
                DeferredSpaceEvent.user_id == proposal.user_id,
                DeferredSpaceEvent.space_id == proposal.space_id,
                In(DeferredSpaceEvent.source_id, sorted(source_ids)),
                In(DeferredSpaceEvent.state, ["pending", "failed", "dispatching"]),
            )
            .sort([("causal_order", 1), ("created_at", 1)])
            .to_list()
        )
        for event in events:
            if (event.source_kind, event.source_id) not in source_refs:
                continue
            event.state = "dispatching"
            event.attempts += 1
            await event.save()
            try:
                metadata = dict(event.metadata)
                metadata["space_deferred_event_id"] = event.event_id
                metadata["space_merge_proposal_id"] = proposal.proposal_id
                metadata["idempotency_key"] = event.idempotency_key
                results = await dispatch_plugin_event(
                    PluginEvent(event.event_type),
                    event.user_id,
                    event.data,
                    metadata,
                    event.description,
                    require_router=True,
                )
                failures = [result for result in (results or []) if not result.success]
                if failures:
                    raise RuntimeError(
                        "; ".join(
                            result.message or "plugin failed" for result in failures
                        )
                    )
                event.state = "dispatched"
                event.dispatched_at = _utcnow()
                event.error = None
            except Exception as exc:
                event.state = "failed"
                event.error = f"{type(exc).__name__}: {exc}"[:2000]
            await event.save()

    async def retry_deferred_event(
        self, user_id: str, space_id: str, event_id: str
    ) -> DeferredSpaceEvent:
        event = await DeferredSpaceEvent.find_one(
            DeferredSpaceEvent.event_id == event_id,
            DeferredSpaceEvent.user_id == user_id,
            DeferredSpaceEvent.space_id == space_id,
        )
        if event is None:
            raise MemoryScopeError("Deferred event not found")
        if event.state not in {"failed", "dispatching"}:
            raise MemorySpaceConflict("Deferred event is not retryable")
        event.state = "dispatching"
        event.attempts += 1
        await event.save()
        try:
            metadata = {
                **event.metadata,
                "space_deferred_event_id": event.event_id,
                "idempotency_key": event.idempotency_key,
            }
            results = await dispatch_plugin_event(
                PluginEvent(event.event_type),
                event.user_id,
                event.data,
                metadata,
                event.description,
                require_router=True,
            )
            failures = [result for result in (results or []) if not result.success]
            if failures:
                raise RuntimeError(
                    "; ".join(result.message or "plugin failed" for result in failures)
                )
            event.state = "dispatched"
            event.dispatched_at = _utcnow()
            event.error = None
        except Exception as exc:
            event.state = "failed"
            event.error = f"{type(exc).__name__}: {exc}"[:2000]
        await event.save()
        return event


memory_space_service = MemorySpaceService()
