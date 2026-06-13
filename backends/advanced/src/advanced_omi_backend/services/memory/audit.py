"""Recorder for the memory vault audit ledger.

Memory is a per-user markdown vault that is overwritten in place rather than
versioned, so instead of keeping prior copies we record *which notes changed,
when, and what triggered the change* into the ``memory_audit`` collection
(see :class:`advanced_omi_backend.models.memory_audit.MemoryAuditEntry`).

Only the chronicle provider owns a vault, so it is the only caller. Recording is
best-effort: a ledger write must never break memory processing.

The ``trigger`` (normal extraction vs. speaker reprocess vs. delete-all) is known
by the memory job, not the provider, so it is passed down through a contextvar
set with :func:`memory_trigger` around the provider call — the provider runs in
the same coroutine, so the value is visible without threading it through the
shared ``add_memory`` signature.
"""

import contextlib
import contextvars
import difflib
import hashlib
import logging
from typing import Any, Iterator, Optional

logger = logging.getLogger("memory_service.audit")

_current_trigger: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "memory_audit_trigger", default=None
)


@contextlib.contextmanager
def memory_trigger(trigger: Optional[str]) -> Iterator[None]:
    """Set the trigger recorded by vault changes within this block."""
    token = _current_trigger.set(trigger)
    try:
        yield
    finally:
        _current_trigger.reset(token)


def _sha256(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_delta_summary(before: Optional[str], after: Optional[str]) -> Optional[str]:
    """A short, human-readable description of how a note changed."""
    if before is None and after is not None:
        return f"created ({len(after.splitlines())} lines)"
    if after is None and before is not None:
        return "deleted"
    if before is None or after is None:
        return None
    if before == after:
        return "no change"
    added = removed = 0
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return f"+{added}/-{removed} lines"


async def record_vault_change(
    *,
    user_id: str,
    operation: str,
    conversation_id: Optional[str] = None,
    note_path: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    agent_mode: bool = False,
    provider: str = "chronicle",
    summary: Optional[str] = None,
    **extra: Any,
) -> None:
    """Append one entry to the memory vault audit ledger (best-effort).

    ``before``/``after`` are note contents used only to derive hashes and a line
    delta — they are not stored. Pass ``after=None`` for deletions.
    """
    # Imported lazily so this module stays importable before Beanie is set up.
    from advanced_omi_backend.models.memory_audit import MemoryAuditEntry

    try:
        entry = MemoryAuditEntry(
            user_id=str(user_id),
            conversation_id=conversation_id,
            operation=operation,
            note_path=note_path,
            trigger=_current_trigger.get(),
            provider=provider,
            agent_mode=agent_mode,
            before_hash=_sha256(before),
            after_hash=_sha256(after),
            after_bytes=len(after.encode("utf-8")) if after is not None else None,
            summary=summary or _line_delta_summary(before, after),
            extra=dict(extra),
        )
        await entry.insert()
    except Exception as e:  # noqa: BLE001 — audit must never break processing
        logger.warning(
            "Failed to record vault audit (%s %s for %s): %s",
            operation,
            note_path,
            user_id,
            e,
        )
