import asyncio
import contextlib
import os
from uuid import uuid4

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.audio_capture import AudioCaptureSession
from backend.models.conversation import Conversation
from backend.models.memory_audit import MemoryAuditEntry
from backend.models.memory_space import (
    DeferredSpaceEvent,
    MemorySpace,
    SpaceMergeProposal,
)
from backend.services import memory_spaces
from backend.services.memory.scope import (
    MemoryScope,
    MemoryScopeError,
    MemoryScopeResolver,
)
from backend.services.memory.vault_scaffold import seed_vault_scaffold
from backend.services.memory.vault_verify import verify_vault_changes
from backend.services.memory_spaces import MemorySpaceConflict, MemorySpaceService

pytestmark = pytest.mark.integration


@pytest.fixture
async def merge_service(mongo_service, tmp_path, monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client[f"test_memory_spaces_{uuid4().hex}"]
    await init_beanie(
        database=database,
        document_models=[
            MemorySpace,
            SpaceMergeProposal,
            DeferredSpaceEvent,
            MemoryAuditEntry,
            Conversation,
            AudioCaptureSession,
        ],
    )
    # Fixture-local import ensures the service observes the initialized Beanie models.
    from backend.services import memory_spaces

    monkeypatch.setattr(
        memory_spaces, "vault_run_lock", lambda _user_id: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        memory_spaces, "verify_vault_changes", lambda _stage, _before: []
    )
    service = MemorySpaceService(MemoryScopeResolver(tmp_path))
    yield service
    await client.drop_database(database.name)
    client.close()


async def test_partial_merge_archives_and_reopen_does_not_repeat_rejected_changes(
    merge_service,
):
    service = merge_service
    space = await service.create("user-1", "Brainstorm")
    scope = MemoryScope("user-1", space.space_id)
    vault = service.resolver.vault_root(scope)
    (vault / "Topics").mkdir(parents=True, exist_ok=True)
    (vault / "Topics" / "Accepted.md").write_text("# Accepted\n", encoding="utf-8")
    (vault / "Topics" / "Rejected.md").write_text("# Rejected\n", encoding="utf-8")

    proposal = await service.prepare_merge("user-1", space.space_id)
    accepted = next(
        change
        for change in proposal.changes
        if change.note_path == "Topics/Accepted.md"
    )
    resolved = await service.resolve_merge(
        "user-1", proposal.proposal_id, [accepted.change_id]
    )

    main = service.resolver.main_root("user-1")
    assert resolved.state == "applied"
    assert (main / "Topics" / "Accepted.md").read_text() == "# Accepted\n"
    assert not (main / "Topics" / "Rejected.md").exists()
    archived = await service.get("user-1", space.space_id)
    assert archived.state == "archived"

    await service.reopen("user-1", space.space_id)
    next_proposal = await service.prepare_merge("user-1", space.space_id)
    assert next_proposal.changes == []


async def test_prepare_merge_real_validator_ignores_unchanged_main_scaffold(
    merge_service, monkeypatch
):
    service = merge_service
    main = service.resolver.main_root("user-1")

    seed_vault_scaffold(main)
    monkeypatch.setattr(memory_spaces, "verify_vault_changes", verify_vault_changes)

    space = await service.create("user-1", "Valid merge")
    scope = MemoryScope("user-1", space.space_id)
    vault = service.resolver.vault_root(scope)
    (vault / "Topics").mkdir(parents=True, exist_ok=True)
    (vault / "Topics" / "Accepted.md").write_text(
        "---\ncategories:\n  - '[[Topics]]'\ncreated: 2026-08-30\n"
        "updated: 2026-08-30\n---\n## About\n- A supported fact.\n\n"
        "## Conversations\n![[Conversations.base#Topic]]\n",
        encoding="utf-8",
    )

    proposal = await service.prepare_merge("user-1", space.space_id)

    assert [change.note_path for change in proposal.changes] == ["Topics/Accepted.md"]


async def test_main_hash_change_makes_entire_selected_batch_stale(merge_service):
    service = merge_service
    space = await service.create("user-1", "Collision")
    scope = MemoryScope("user-1", space.space_id)
    vault = service.resolver.vault_root(scope)
    (vault / "Topics").mkdir(parents=True, exist_ok=True)
    (vault / "Topics" / "One.md").write_text("# One from space\n", encoding="utf-8")
    (vault / "Topics" / "Two.md").write_text("# Two from space\n", encoding="utf-8")
    proposal = await service.prepare_merge("user-1", space.space_id)

    main = service.resolver.main_root("user-1")
    (main / "Topics").mkdir(parents=True, exist_ok=True)
    (main / "Topics" / "One.md").write_text("# Main changed\n", encoding="utf-8")

    with pytest.raises(MemorySpaceConflict, match="changed after"):
        await service.resolve_merge(
            "user-1",
            proposal.proposal_id,
            [change.change_id for change in proposal.changes],
        )

    assert (main / "Topics" / "One.md").read_text() == "# Main changed\n"
    assert not (main / "Topics" / "Two.md").exists()


async def test_cancel_pending_merge_returns_space_to_editing_without_publishing(
    merge_service,
):
    service = merge_service
    space = await service.create("user-1", "Needs device sync")
    scope = MemoryScope("user-1", space.space_id)
    vault = service.resolver.vault_root(scope)
    (vault / "Topics").mkdir(parents=True, exist_ok=True)
    (vault / "Topics" / "Private.md").write_text("# Still private\n", encoding="utf-8")
    proposal = await service.prepare_merge("user-1", space.space_id)

    cancelled = await service.cancel_merge("user-1", proposal.proposal_id)

    assert cancelled.state == "cancelled"
    assert cancelled.resolved_at is not None
    assert (await service.get("user-1", space.space_id)).state == "active"
    assert not (service.resolver.main_root("user-1") / "Topics" / "Private.md").exists()
    replacement = await service.prepare_merge("user-1", space.space_id)
    assert replacement.state == "pending"


async def test_cancel_and_resolve_race_has_one_terminal_outcome(merge_service):
    service = merge_service
    space = await service.create("user-1", "Race")
    scope = MemoryScope("user-1", space.space_id)
    vault = service.resolver.vault_root(scope)
    (vault / "Topics").mkdir(parents=True, exist_ok=True)
    (vault / "Topics" / "Race.md").write_text("# Race\n", encoding="utf-8")
    proposal = await service.prepare_merge("user-1", space.space_id)

    results = await asyncio.gather(
        service.resolve_merge(
            "user-1",
            proposal.proposal_id,
            [change.change_id for change in proposal.changes],
        ),
        service.cancel_merge("user-1", proposal.proposal_id),
        return_exceptions=True,
    )

    assert sum(isinstance(result, MemorySpaceConflict) for result in results) == 1
    live_proposal = await SpaceMergeProposal.find_one(
        SpaceMergeProposal.proposal_id == proposal.proposal_id
    )
    live_space = await service.get("user-1", space.space_id)
    published = service.resolver.main_root("user-1") / "Topics" / "Race.md"
    assert (live_proposal.state, live_space.state, published.exists()) in {
        ("applied", "archived", True),
        ("cancelled", "active", False),
    }


async def test_cancel_is_owner_fenced_and_idempotently_repairs_space(merge_service):
    service = merge_service
    space = await service.create("user-1", "Repair")
    proposal = await service.prepare_merge("user-1", space.space_id)

    with pytest.raises(MemorySpaceConflict):
        await service.cancel_merge("user-2", proposal.proposal_id)

    await SpaceMergeProposal.get_pymongo_collection().update_one(
        {"proposal_id": proposal.proposal_id},
        {"$set": {"state": "cancelled"}},
    )
    repaired = await service.cancel_merge("user-1", proposal.proposal_id)

    assert repaired.state == "cancelled"
    assert (await service.get("user-1", space.space_id)).state == "active"


async def test_latest_proposal_ignores_terminal_ledger_from_prior_cycle(merge_service):
    service = merge_service
    space = await service.create("user-1", "Cycles")
    old_proposal = await service.prepare_merge("user-1", space.space_id)
    await service.cancel_merge("user-1", old_proposal.proposal_id)

    live_space = await service.get("user-1", space.space_id)
    live_space.state = "merging"
    live_space.active_merge_proposal_id = "current-proposal"
    live_space.updated_at = memory_spaces._utcnow()
    await live_space.save()

    assert await service.latest_merge_proposal("user-1", space.space_id) is None

    current = SpaceMergeProposal(
        proposal_id="current-proposal",
        user_id="user-1",
        space_id=space.space_id,
        state="pending",
    )
    await current.insert()
    found = await service.latest_merge_proposal("user-1", space.space_id)
    assert found is not None
    assert found.proposal_id == current.proposal_id


async def test_retrying_old_cancel_cannot_reopen_new_merge_cycle(merge_service):
    service = merge_service
    space = await service.create("user-1", "Cycle fence")
    first = await service.prepare_merge("user-1", space.space_id)
    await service.cancel_merge("user-1", first.proposal_id)
    second = await service.prepare_merge("user-1", space.space_id)

    with pytest.raises(MemorySpaceConflict):
        await service.cancel_merge("user-1", first.proposal_id)

    live_space = await service.get("user-1", space.space_id)
    live_second = await SpaceMergeProposal.find_one(
        SpaceMergeProposal.proposal_id == second.proposal_id
    )
    assert live_space.state == "merging"
    assert live_space.active_merge_proposal_id == second.proposal_id
    assert live_second.state == "pending"


async def test_validator_rejection_does_not_persist_a_pending_proposal(
    merge_service, monkeypatch
):
    service = merge_service
    space = await service.create("user-1", "Invalid")
    scope = MemoryScope("user-1", space.space_id)
    vault = service.resolver.vault_root(scope)
    (vault / "Topics").mkdir(parents=True, exist_ok=True)
    (vault / "Topics" / "Bad.md").write_text("# Bad\n", encoding="utf-8")
    monkeypatch.setattr(
        memory_spaces,
        "verify_vault_changes",
        lambda _stage, _before: [
            type(
                "Finding",
                (),
                {"path": "Topics/Bad.md", "rule": "bad", "detail": "invalid"},
            )()
        ],
    )

    with pytest.raises(MemorySpaceConflict, match="failed validation"):
        await service.prepare_merge("user-1", space.space_id)

    assert (
        await SpaceMergeProposal.find(
            SpaceMergeProposal.space_id == space.space_id
        ).count()
        == 0
    )
    assert (await service.get("user-1", space.space_id)).state == "active"


async def test_prepare_merge_blocks_new_writes_but_allows_admitted_processing_to_drain(
    merge_service, monkeypatch
):
    service = merge_service
    space = await service.create("user-1", "Drain in flight")
    scope = MemoryScope("user-1", space.space_id)

    async def observe_drain(_user_id: str, _space_id: str) -> None:
        assert (await service.get("user-1", space.space_id)).state == "merging"
        with pytest.raises(MemoryScopeError, match="not active"):
            await service.resolver.require_space(scope, writable=True)
        admitted = await service.resolver.require_space(
            scope,
            writable=True,
            allow_merging=True,
        )
        assert admitted.space_id == space.space_id

    monkeypatch.setattr(service, "_await_processing_drain", observe_drain)

    proposal = await service.prepare_merge("user-1", space.space_id)

    assert proposal.state == "pending"
