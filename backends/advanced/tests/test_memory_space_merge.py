import contextlib
import os
from uuid import uuid4

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.audio_capture import AudioCaptureSession
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.memory_audit import MemoryAuditEntry
from advanced_omi_backend.models.memory_space import (
    DeferredSpaceEvent,
    MemorySpace,
    SpaceMergeProposal,
)
from advanced_omi_backend.services.memory.scope import (
    MemoryScope,
    MemoryScopeError,
    MemoryScopeResolver,
)
from advanced_omi_backend.services.memory_spaces import (
    MemorySpaceConflict,
    MemorySpaceService,
)

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
    from advanced_omi_backend.services import memory_spaces

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


async def test_validator_rejection_does_not_persist_a_pending_proposal(
    merge_service, monkeypatch
):
    service = merge_service
    space = await service.create("user-1", "Invalid")
    scope = MemoryScope("user-1", space.space_id)
    vault = service.resolver.vault_root(scope)
    (vault / "Topics").mkdir(parents=True, exist_ok=True)
    (vault / "Topics" / "Bad.md").write_text("# Bad\n", encoding="utf-8")
    # Import locally so this test patches the same initialized service module.
    from advanced_omi_backend.services import memory_spaces

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
