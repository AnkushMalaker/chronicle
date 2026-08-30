"""Human-gated, chronological Timeline memory review.

Episode generations can be prepared for many days independently. Semantic memory does
not get that freedom: only the oldest reviewed day may produce a proposal, and the next
day cannot run until a person accepts or rejects that proposal. Each proposal is made
in a temporary copy of the live vault, so unapproved text is never visible to later
days or to memory retrieval.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from contextlib import nullcontext
from datetime import datetime, time
from pathlib import Path
from typing import Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from advanced_omi_backend.models.timeline import (
    MemoryReviewProposal,
    PotentialMemoryChange,
    TimelineDay,
    TimelineEpisode,
    utcnow,
)
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.services.memory.audit import (
    MemoryCause,
    UpdateStrategy,
    memory_provenance,
    record_vault_change,
    suppress_memory_audit,
)
from advanced_omi_backend.services.memory.base import DayWriteOutcome
from advanced_omi_backend.services.memory.providers.chronicle import (
    MemoryService as ChronicleMemoryService,
)
from advanced_omi_backend.services.memory.vault_lock import vault_run_lock
from advanced_omi_backend.services.memory.vault_manager import ConvDocVaultManager
from advanced_omi_backend.services.memory.vault_scaffold import is_scaffold_note

from .memory import build_day_digest, build_day_index_digest
from .recording_refs import episode_conversation_ids

logger = logging.getLogger(__name__)


class MemoryReviewError(RuntimeError):
    """The review state or vault fence made the requested transition unsafe."""


class VaultFenceConflict(MemoryReviewError):
    """A selected note no longer matches the proposal's accepted-vault snapshot."""


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


def _snapshot_hash(snapshot: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for note_path, text in sorted(snapshot.items()):
        digest.update(note_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_accepted_vault_sync(user_id: str, live_root: Path, stage_root: Path):
    """Take a consistent copy while ordinary vault writers are excluded."""

    with vault_run_lock(user_id):
        if live_root.exists():
            shutil.copytree(live_root, stage_root)
        else:
            stage_root.mkdir(parents=True)
        snapshot = _snapshot(stage_root)
    return snapshot


def _accepted_vault_hash_sync(user_id: str, live_root: Path) -> str:
    with vault_run_lock(user_id):
        return _snapshot_hash(_snapshot(live_root))


def _summary(before: Optional[str], after: Optional[str]) -> str:
    if before is None:
        return f"Create {len((after or '').splitlines())} lines"
    if after is None:
        return f"Delete {len(before.splitlines())} lines"
    before_lines, after_lines = before.splitlines(), after.splitlines()
    return f"Update {len(before_lines)} → {len(after_lines)} lines"


def build_potential_changes(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    source_episode_keys_by_path: Optional[Mapping[str, Iterable[str]]] = None,
) -> list[PotentialMemoryChange]:
    """Return a stable, reviewable vault diff."""

    changes: list[PotentialMemoryChange] = []
    for note_path in sorted(set(before) | set(after)):
        old, new = before.get(note_path), after.get(note_path)
        if old == new:
            continue
        operation = "create" if old is None else "delete" if new is None else "update"
        changes.append(
            PotentialMemoryChange(
                note_path=note_path,
                operation=operation,
                before_hash=_hash(old),
                after_hash=_hash(new),
                before_text=old,
                after_text=new,
                summary=_summary(old, new),
                source_episode_keys=list(
                    dict.fromkeys(
                        (source_episode_keys_by_path or {}).get(note_path, ())
                    )
                ),
            )
        )
    return changes


async def _earlier_unresolved(day: TimelineDay) -> Optional[TimelineDay]:
    encoded = datetime.combine(day.local_date, time.min)
    return (
        await TimelineDay.find(
            {
                "user_id": day.user_id,
                "timezone": day.timezone,
                "local_date": {"$lt": encoded},
                "active_run_id": {"$nin": [None, ""]},
                "review_state": {"$ne": "finalized"},
            }
        )
        .sort("local_date")
        .first_or_none()
    )


async def _proposal_for(day: TimelineDay) -> MemoryReviewProposal:
    existing = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.user_id == day.user_id,
        MemoryReviewProposal.local_date == day.local_date,
        MemoryReviewProposal.timezone == day.timezone,
        MemoryReviewProposal.timeline_run_id == day.review_run_id,
    )
    if existing is not None:
        existing.state = "generating"
        existing.changes = []
        existing.accepted_change_ids = []
        existing.rejected_change_ids = []
        existing.vault_base_hash = None
        existing.error = None
        existing.generated_at = None
        existing.resolved_at = None
        await existing.save()
        return existing
    proposal = MemoryReviewProposal(
        user_id=day.user_id,
        local_date=day.local_date,
        timezone=day.timezone,
        timeline_run_id=day.review_run_id or "",
    )
    await proposal.insert()
    return proposal


async def generate_memory_review(day: TimelineDay) -> str:
    """Generate one day's proposal against a disposable copy of the accepted vault."""

    blocking = await _earlier_unresolved(day)
    if blocking is not None:
        return f"blocked:{blocking.local_date.isoformat()}"
    if not day.active_run_id or day.review_run_id != day.active_run_id:
        raise MemoryReviewError(
            "Episode review no longer matches the active Timeline run"
        )

    collection = TimelineDay.get_pymongo_collection()
    claimed = await collection.find_one_and_update(
        {
            "user_id": day.user_id,
            "local_date": datetime.combine(day.local_date, time.min),
            "timezone": day.timezone,
            "active_run_id": day.active_run_id,
            "review_run_id": day.review_run_id,
            "review_state": {"$in": ["memory_queued", "failed"]},
        },
        {
            "$set": {
                "review_state": "memory_generating",
                "review_error": None,
            }
        },
    )
    if claimed is None:
        return "busy"

    proposal = await _proposal_for(day)
    await collection.update_one(
        {"_id": claimed["_id"], "review_run_id": day.review_run_id},
        {"$set": {"memory_review_proposal_id": proposal.proposal_id}},
    )

    try:
        episodes = await TimelineEpisode.find(
            TimelineEpisode.user_id == day.user_id,
            TimelineEpisode.run_id == day.active_run_id,
            {"status": {"$ne": "superseded"}},
        ).to_list()
        if not episodes:
            raise MemoryReviewError("The reviewed Timeline run has no episodes")

        semantic_groups = [
            group
            for group in getattr(day, "semantic_groups", [])
            if group.run_id == day.active_run_id
        ]
        digest, _ = build_day_digest(
            episodes,
            day.local_date,
            day.timezone,
            semantic_groups=semantic_groups,
        )
        index_digest = build_day_index_digest(episodes, day.local_date, day.timezone)
        live_service = get_memory_service()
        if not isinstance(live_service, ChronicleMemoryService):
            raise MemoryReviewError("Timeline review requires the Chronicle vault")
        live_root = live_service.vault.user_root(day.user_id)
        synthetic_user = f"review-{proposal.proposal_id}"

        with tempfile.TemporaryDirectory(prefix="chronicle-memory-review-") as tmp:
            stage_base = Path(tmp) / "conversation_docs"
            stage_root = stage_base / synthetic_user
            before = await asyncio.to_thread(
                _copy_accepted_vault_sync, day.user_id, live_root, stage_root
            )
            proposal.vault_base_hash = _snapshot_hash(before)
            await proposal.save()
            staged_service = ChronicleMemoryService(live_service.config)
            staged_service.vault = ConvDocVaultManager(stage_base)
            with suppress_memory_audit():
                outcome, _ = await staged_service.add_day_memory(
                    digest,
                    day.local_date.isoformat(),
                    synthetic_user,
                    day_index_digest=index_digest,
                    source_date=datetime.combine(
                        day.local_date, time.min, tzinfo=ZoneInfo(day.timezone)
                    ).isoformat(),
                    source_run_id=day.active_run_id,
                    source_episode_ids=[episode.episode_id for episode in episodes],
                    source_conversation_ids=list(
                        dict.fromkeys(
                            conversation_id
                            for episode in episodes
                            for conversation_id in episode_conversation_ids(episode)
                        )
                    ),
                )
            if outcome is not DayWriteOutcome.COMPLETE:
                raise MemoryReviewError(
                    f"Candidate memory agent ended with {outcome.value}"
                )
            all_episode_keys = list(
                dict.fromkeys(episode.episode_key for episode in episodes)
            )
            provenance = dict(staged_service.last_day_source_episode_keys_by_path)
            provenance[f"Daily/{day.local_date.isoformat()}.md"] = all_episode_keys
            changes = build_potential_changes(
                before,
                _snapshot(stage_root),
                source_episode_keys_by_path=provenance,
            )
            allowed_episode_keys = set(all_episode_keys)
            for change in changes:
                unknown = set(change.source_episode_keys) - allowed_episode_keys
                if unknown:
                    raise MemoryReviewError(
                        f"{change.note_path} cited episodes outside the reviewed day"
                    )
                if not change.source_episode_keys:
                    raise MemoryReviewError(
                        f"{change.note_path} has no supporting Timeline episode"
                    )

        # A no-op is a decision too. Confirm it was made against the vault that still
        # exists before allowing the queue to advance automatically.
        if not changes:
            current_hash = await asyncio.to_thread(
                _accepted_vault_hash_sync, day.user_id, live_root
            )
            if current_hash != proposal.vault_base_hash:
                raise MemoryReviewError(
                    "The vault changed while the no-change proposal was generated"
                )

        current = await TimelineDay.find_one(
            TimelineDay.user_id == day.user_id,
            TimelineDay.local_date == day.local_date,
            TimelineDay.timezone == day.timezone,
        )
        if (
            current is None
            or current.active_run_id != day.review_run_id
            or current.review_state != "memory_generating"
        ):
            proposal.state = "stale"
            proposal.error = "Timeline changed while the proposal was generated"
            await proposal.save()
            return "stale"

        proposal.changes = changes
        proposal.generated_at = utcnow()
        proposal.state = "pending" if changes else "no_changes"
        await proposal.save()
        await collection.update_one(
            {
                "_id": current.id,
                "active_run_id": day.review_run_id,
                "review_state": "memory_generating",
            },
            {
                "$set": {
                    "review_state": "memory_pending" if changes else "finalized",
                    "review_outcome": None if changes else "no_changes",
                    "review_resolved_at": None if changes else utcnow(),
                    "review_error": None,
                }
            },
        )
        return proposal.state
    except Exception as exc:
        diagnostic = f"{type(exc).__name__}: {exc}"[:2000]
        proposal.state = "failed"
        proposal.error = diagnostic
        await proposal.save()
        await collection.update_one(
            {
                "_id": claimed["_id"],
                "active_run_id": day.review_run_id,
                "review_state": "memory_generating",
            },
            {"$set": {"review_state": "failed", "review_error": diagnostic}},
        )
        logger.error(
            "Memory review generation failed for %s", day.local_date, exc_info=True
        )
        return "failed"


def _safe_note(root: Path, note_path: str) -> Path:
    relative = Path(note_path)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
        raise MemoryReviewError(f"Unsafe proposed note path: {note_path}")
    target = (root / relative).resolve()
    resolved_root = root.resolve()
    if resolved_root != target and resolved_root not in target.parents:
        raise MemoryReviewError(f"Proposed note escapes the vault: {note_path}")
    return target


def _apply_changes_sync(
    user_id: str,
    root: Path,
    changes: Iterable[PotentialMemoryChange],
) -> list[tuple[PotentialMemoryChange, Optional[str]]]:
    """Fence and apply selected files under Chronicle's cross-process vault lock."""

    selected = list(changes)
    prior: dict[Path, Optional[str]] = {}
    context = vault_run_lock(user_id) if selected else nullcontext()
    with context:
        for change in selected:
            target = _safe_note(root, change.note_path)
            current = target.read_text(encoding="utf-8") if target.is_file() else None
            if _hash(current) != change.before_hash:
                raise VaultFenceConflict(
                    f"{change.note_path} changed after this proposal was generated"
                )
            prior[target] = current

        try:
            for change in selected:
                target = _safe_note(root, change.note_path)
                if change.after_text is None:
                    if target.exists():
                        target.unlink()
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(change.after_text, encoding="utf-8")
                os.replace(temporary, target)
        except Exception:
            for target, text in prior.items():
                if text is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
            raise
    return [(change, prior[_safe_note(root, change.note_path)]) for change in selected]


async def resolve_memory_review(
    proposal: MemoryReviewProposal, accepted_change_ids: Iterable[str]
) -> str:
    """Apply an accepted subset, or discard the proposal, then unblock the next day."""

    accepted = set(accepted_change_ids)
    known = {change.change_id for change in proposal.changes}
    if not accepted.issubset(known):
        raise MemoryReviewError("The decision includes a change outside this proposal")
    if proposal.state != "pending":
        raise MemoryReviewError("This proposal is no longer pending review")
    live_service = get_memory_service()
    if not isinstance(live_service, ChronicleMemoryService):
        raise MemoryReviewError("Timeline review requires the Chronicle vault")

    proposal_collection = MemoryReviewProposal.get_pymongo_collection()
    proposal_claimed = await proposal_collection.find_one_and_update(
        {"proposal_id": proposal.proposal_id, "state": "pending"},
        {"$set": {"state": "applying", "error": None}},
    )
    if proposal_claimed is None:
        raise MemoryReviewError("This proposal is already being resolved")
    proposal.state = "applying"

    day = await TimelineDay.find_one(
        TimelineDay.user_id == proposal.user_id,
        TimelineDay.local_date == proposal.local_date,
        TimelineDay.timezone == proposal.timezone,
    )
    if (
        day is None
        or day.active_run_id != proposal.timeline_run_id
        or day.review_run_id != proposal.timeline_run_id
        or day.review_state != "memory_pending"
    ):
        proposal.state = "stale"
        proposal.error = "Timeline changed before the proposal was reviewed"
        await proposal.save()
        raise MemoryReviewError(proposal.error)

    day_claimed = await TimelineDay.get_pymongo_collection().find_one_and_update(
        {
            "user_id": proposal.user_id,
            "local_date": datetime.combine(proposal.local_date, time.min),
            "timezone": proposal.timezone,
            "active_run_id": proposal.timeline_run_id,
            "review_run_id": proposal.timeline_run_id,
            "review_state": "memory_pending",
        },
        {"$set": {"review_state": "memory_applying", "review_error": None}},
    )
    if day_claimed is None:
        await proposal_collection.update_one(
            {"proposal_id": proposal.proposal_id, "state": "applying"},
            {"$set": {"state": "pending"}},
        )
        proposal.state = "pending"
        raise MemoryReviewError("This day changed before its proposal could be claimed")

    selected = [change for change in proposal.changes if change.change_id in accepted]
    try:
        source_episodes = await TimelineEpisode.find(
            TimelineEpisode.user_id == proposal.user_id,
            TimelineEpisode.run_id == proposal.timeline_run_id,
            {"status": {"$ne": "superseded"}},
        ).to_list()
        source_episode_ids = tuple(episode.episode_id for episode in source_episodes)
        source_conversation_ids = tuple(
            dict.fromkeys(
                conversation_id
                for episode in source_episodes
                for conversation_id in episode_conversation_ids(episode)
            )
        )
        applied = await asyncio.to_thread(
            _apply_changes_sync,
            proposal.user_id,
            live_service.vault.user_root(proposal.user_id),
            selected,
        )
    except Exception as exc:
        diagnostic = f"{type(exc).__name__}: {exc}"[:2000]
        stale = isinstance(exc, VaultFenceConflict)
        await proposal_collection.update_one(
            {"proposal_id": proposal.proposal_id, "state": "applying"},
            {"$set": {"state": "stale" if stale else "pending", "error": diagnostic}},
        )
        await TimelineDay.get_pymongo_collection().update_one(
            {
                "_id": day_claimed["_id"],
                "active_run_id": proposal.timeline_run_id,
                "review_state": "memory_applying",
            },
            {
                "$set": {
                    "review_state": "failed" if stale else "memory_pending",
                    "review_error": diagnostic,
                }
            },
        )
        proposal.state = "stale" if stale else "pending"
        proposal.error = diagnostic
        raise

    with memory_provenance(
        MemoryCause.DAY_EPISODES.value,
        UpdateStrategy.FULL.value,
        source_type="timeline_day",
        source_id=proposal.local_date.isoformat(),
        source_conversation_ids=source_conversation_ids,
        source_episode_ids=source_episode_ids,
        timeline_run_id=proposal.timeline_run_id,
    ):
        for change, before in applied:
            await record_vault_change(
                user_id=proposal.user_id,
                operation=change.operation,
                note_path=change.note_path,
                before=before,
                after=change.after_text,
                # The agent authored the proposal; a person caused this live write by
                # accepting it. Keep the ledger actor human and retain the proposal id.
                agent_mode=False,
                summary=change.summary,
                review_proposal_id=proposal.proposal_id,
                relevant_episode_keys=change.source_episode_keys,
            )

    rejected = known - accepted
    outcome = "applied" if accepted else "rejected"
    proposal.state = outcome
    proposal.accepted_change_ids = sorted(accepted)
    proposal.rejected_change_ids = sorted(rejected)
    proposal.resolved_at = utcnow()
    proposal.error = None
    await proposal.save()
    await TimelineDay.get_pymongo_collection().update_one(
        {
            "user_id": proposal.user_id,
            "local_date": datetime.combine(proposal.local_date, time.min),
            "timezone": proposal.timezone,
            "active_run_id": proposal.timeline_run_id,
            "review_state": "memory_applying",
        },
        {
            "$set": {
                "review_state": "finalized",
                "review_outcome": outcome,
                "review_resolved_at": utcnow(),
                "review_error": None,
                "memory_state": "written" if accepted else "no_changes",
                "memory_run_id": proposal.timeline_run_id,
                "memory_written_at": utcnow(),
            }
        },
    )
    await TimelineEpisode.get_pymongo_collection().update_many(
        {"user_id": proposal.user_id, "run_id": proposal.timeline_run_id},
        {
            "$set": {
                "memory_state": "written" if accepted else "skipped",
                "vault_paths": [change.note_path for change in selected],
            }
        },
    )
    return outcome


async def queue_memory_review_regeneration(
    proposal: MemoryReviewProposal,
) -> TimelineDay:
    """Invalidate a stale diff and queue the same day against the current vault."""

    if proposal.state not in {"pending", "failed", "stale"}:
        raise MemoryReviewError(
            "This proposal cannot be regenerated in its current state"
        )
    day = await TimelineDay.find_one(
        TimelineDay.user_id == proposal.user_id,
        TimelineDay.local_date == proposal.local_date,
        TimelineDay.timezone == proposal.timezone,
    )
    if (
        day is None
        or day.active_run_id != proposal.timeline_run_id
        or day.review_run_id != proposal.timeline_run_id
        or day.review_state not in {"memory_pending", "failed"}
    ):
        raise MemoryReviewError(
            "This day changed before its proposal could be regenerated"
        )

    proposal.state = "stale"
    proposal.error = "Regenerating against the current accepted vault"
    await proposal.save()
    day.review_state = "memory_queued"
    day.review_error = None
    await day.save()
    return day


async def process_memory_review_queue() -> dict[str, int]:
    """Generate the oldest eligible proposal for each user.

    One unresolved proposal per user is intentional. It prevents a later day from
    observing text that a person might reject.
    """

    totals = {"considered": 0, "pending": 0, "no_changes": 0, "failed": 0}
    candidates = (
        await TimelineDay.find(
            {"active_run_id": {"$nin": [None, ""]}, "review_state": "memory_queued"}
        )
        .sort("local_date")
        .to_list()
    )
    seen_users: set[str] = set()
    for day in candidates:
        if day.user_id in seen_users:
            continue
        seen_users.add(day.user_id)
        totals["considered"] += 1
        outcome = await generate_memory_review(day)
        if outcome in totals:
            totals[outcome] += 1
    return totals
