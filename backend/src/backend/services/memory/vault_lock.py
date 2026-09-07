"""Cross-process lock serialising individual vault writes per user.

Six RQ workers all serve the ``memory`` queue, so two memory jobs for the SAME user can
run concurrently in different worker processes. Each mutating vault operation (write,
edit, rename, category creation) is a read-resolve-write sequence that must be atomic:
without a lock, two concurrent runs can each pass ``VaultTools``' exists/case-collision
checks before either has written, minting duplicate or case-variant sibling notes that
are irrecoverable on case-insensitive Syncthing clients.

The lock is held only for the duration of ONE filesystem operation (milliseconds — or a
few seconds for ``rename_person``, which rewrites backlinks across the vault), never
across LLM calls. Concurrent agent runs therefore interleave at note granularity;
staleness between an agent's read and its write is caught by ``edit_engine``'s exact
``old_text`` anchoring, which fails loudly so the model re-reads and retries.

Fails CLOSED: if the lock cannot be acquired within the wait window the operation raises
and the error is surfaced to the agent, which can simply retry the tool call. With
millisecond holds the wait window is generous; and since RQ jobs themselves live on this
same Redis, "Redis is down" cannot be a reason to proceed unlocked — the job would not
be running. (The previous whole-run lock failed OPEN after a 10-minute wait, which under
bulk-import contention let an unlocked agent interleave with locked ones and corrupt
notes.)
"""

import contextlib
import logging
from typing import Iterator

from ...redis_factory import create_sync_redis

logger = logging.getLogger("memory_service.vault")

# Auto-release TTL — must exceed the longest single vault operation (rename_person
# rewriting backlinks across a large vault), bounded so a crashed holder cannot wedge a
# user's vault for long.
_LOCK_TTL_SECONDS = 60
# How long one operation blocks for the lock before failing (closed). Holds are
# milliseconds, so hitting this means something is genuinely wrong.
_LOCK_WAIT_SECONDS = 30


class VaultLockTimeout(Exception):
    """The per-user vault lock could not be acquired within the wait window."""


@contextlib.contextmanager
def vault_run_lock(user_id: str, ttl_seconds: int = 1200) -> Iterator[None]:
    """Hold the per-user vault lock for a whole external-executor run.

    The Codex CLI executor edits vault files directly for the duration of one agent
    run (minutes, not milliseconds), so it takes the SAME ``vault:write:{user_id}``
    key with a run-scale TTL instead of the per-operation one. While it is held,
    per-operation writers (``vault_note_lock``) block for their 30s window and then
    fail closed with a retryable error — acceptable because memory jobs are already
    serialised on one worker, so contention is limited to rare chat-driven writes.
    """
    client = create_sync_redis(decode_responses=True)
    lock = client.lock(
        f"vault:write:{user_id}",
        timeout=ttl_seconds,
        blocking_timeout=_LOCK_WAIT_SECONDS,
    )
    try:
        if not lock.acquire():
            raise VaultLockTimeout(
                f"vault run lock for user {user_id} not acquired within "
                f"{_LOCK_WAIT_SECONDS}s"
            )
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                lock.release()
    finally:
        with contextlib.suppress(Exception):
            client.close()


@contextlib.contextmanager
def vault_note_lock(user_id: str) -> Iterator[None]:
    """Hold the per-user lock around ONE vault-mutating filesystem operation.

    Raises :class:`VaultLockTimeout` if the lock cannot be acquired — callers surface
    this to the agent as a retryable tool error.
    """
    client = create_sync_redis(decode_responses=True)
    lock = client.lock(
        f"vault:write:{user_id}",
        timeout=_LOCK_TTL_SECONDS,
        blocking_timeout=_LOCK_WAIT_SECONDS,
    )
    try:
        if not lock.acquire():
            raise VaultLockTimeout(
                f"vault lock for user {user_id} not acquired within "
                f"{_LOCK_WAIT_SECONDS}s"
            )
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                lock.release()
    finally:
        with contextlib.suppress(Exception):
            client.close()
