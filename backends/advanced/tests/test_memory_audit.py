"""Source identity contracts for the append-only memory audit ledger."""

import pytest

from advanced_omi_backend.services.memory import audit
from advanced_omi_backend.services.memory.audit import (
    MemoryCause,
    UpdateStrategy,
    memory_provenance,
    record_vault_change,
)


@pytest.mark.asyncio
async def test_timeline_day_audit_uses_typed_source_metadata_not_conversation_id(
    monkeypatch,
):
    captured = []

    class FakeEntry:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        async def insert(self):
            return None

    monkeypatch.setattr(audit, "MemoryAuditEntry", FakeEntry)

    with memory_provenance(
        MemoryCause.DAY_EPISODES,
        UpdateStrategy.FULL,
        source_type="timeline_day",
        source_id="2026-08-06",
        source_conversation_ids=("conversation-one", "conversation-two"),
        source_episode_ids=("episode-one",),
        timeline_run_id="run-one",
    ):
        await record_vault_change(
            user_id="user-one",
            operation="update",
            conversation_id="2026-08-06",
            note_path="Daily/2026-08-06.md",
            before="before",
            after="after",
        )

    assert len(captured) == 1
    entry = captured[0]
    assert entry["conversation_id"] is None
    assert entry["extra"] == {
        "source_type": "timeline_day",
        "source_id": "2026-08-06",
        "source_conversation_ids": ["conversation-one", "conversation-two"],
        "source_episode_ids": ["episode-one"],
        "timeline_run_id": "run-one",
    }
