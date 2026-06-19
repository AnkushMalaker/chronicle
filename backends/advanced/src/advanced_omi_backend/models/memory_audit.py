"""Memory vault audit ledger.

An append-only record of every change made to a user's memory vault
(``data/conversation_docs/{user_id}/``). This is the audit trail that replaced
the old per-conversation "memory versions": memory is now a filesystem vault that
is overwritten in place, so instead of versioning the data we record *which notes
changed, when, and what triggered the change*.

Entries are written by the chronicle memory provider (the only provider that owns
a vault) from the memory RQ worker, and read back through the API for display.
Each entry retains the post-change note content (``after_text``) so the ledger can
show an exact before→after diff: the "before" is reconstructed from the previous
recorded change to the same note. There is no restore — the ledger records history.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class MemoryAuditEntry(Document):
    """One recorded change to a user's memory vault."""

    user_id: Indexed(str) = Field(description="Owner of the vault that changed")
    conversation_id: Optional[str] = Field(
        None, description="Conversation whose processing caused the change (if any)"
    )

    # What happened
    operation: str = Field(description="create | update | delete | rename | delete_all")
    note_path: Optional[str] = Field(
        None,
        description="Vault-relative path of the changed note (e.g. 'People/Alice.md')",
    )
    cause: Optional[str] = Field(
        None,
        description="Why the memory changed (provenance), one of MemoryCause: "
        "auto_extraction, memory_replay, transcript_reprocess, speaker_reprocess, "
        "annotation_apply, obsidian_sync, delete_all. See services/memory/audit.py.",
    )
    strategy: Optional[str] = Field(
        None,
        description="How the provider updated the vault (UpdateStrategy): "
        "full re-extraction or speaker_diff. Control-flow detail, not a label.",
    )
    provider: str = Field(
        default="chronicle", description="Memory provider that owns the vault"
    )
    agent_mode: bool = Field(
        default=False,
        description="Whether the vault-first memory agent produced this change",
    )

    # What changed: hashes/sizes for integrity, plus the post-change content so a
    # before→after diff can be reconstructed from the note's recorded history.
    before_hash: Optional[str] = Field(
        None,
        description="SHA-256 of the note before the change (None if newly created)",
    )
    after_hash: Optional[str] = Field(
        None, description="SHA-256 of the note after the change (None if deleted)"
    )
    after_bytes: Optional[int] = Field(
        None, description="Size of the note after the change, in bytes"
    )
    after_text: Optional[str] = Field(
        None,
        description="Full note content after the change (None if deleted). Used to "
        "reconstruct diffs; the 'before' is the prior recorded change to this note.",
    )
    summary: Optional[str] = Field(
        None,
        description="Human-readable note about the change (line delta, agent summary, etc.)",
    )
    extra: Dict[str, Any] = Field(
        default_factory=dict, description="Additional operation-specific metadata"
    )

    created_at: Indexed(datetime) = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the change was recorded",
    )

    class Settings:
        name = "memory_audit"
        indexes = [
            "user_id",
            "conversation_id",
            "created_at",
            IndexModel(
                [("conversation_id", 1), ("created_at", -1)],
                name="memory_audit_conversation_recent",
            ),
            IndexModel(
                [("user_id", 1), ("created_at", -1)],
                name="memory_audit_user_recent",
            ),
        ]
