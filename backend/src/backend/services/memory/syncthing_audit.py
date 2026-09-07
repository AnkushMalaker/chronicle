"""Record inbound (human/Obsidian) vault edits into the memory audit ledger.

The chronicle provider records the changes the *system* makes to the vault. The
vault is also edited directly in Obsidian and delivered to the backend by
Syncthing — those never pass through the provider. This listener captures them.

It long-polls the server-side Syncthing's ``ItemFinished`` events. On the backend
node that event only fires for items pulled *from a remote device* — our own
writes are local changes that propagate outward — so every ``ItemFinished`` here
is, by construction, a change a human made elsewhere. Each one becomes a
``MemoryAuditEntry`` with ``trigger="obsidian_sync"``.

Runs as a single background task in the API process (where the Syncthing
credentials live) and no-ops when vault sync is not configured.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from backend.redis_factory import create_async_redis

from .audit import MemoryCause, memory_provenance, record_vault_change

logger = logging.getLogger("memory_service.audit")

# Same Syncthing instance the vault-sync broker drives (internal docker network).
_SYNCTHING_URL = os.getenv("VAULT_SYNC_SYNCTHING_URL", "http://vault-syncthing:8384")
_SYNCTHING_API_KEY = os.getenv("VAULT_SYNC_API_KEY", "")
# Folder ids are "vault-{user_id}" (see vault_sync_routes._folder_id).
_FOLDER_PREFIX = "vault-"
# The vault on disk, as seen by THIS (backend) process.
_BACKEND_VAULTS_DIR = Path(os.getenv("DATA_DIR", "/app/data")) / "conversation_docs"

# Redis key persisting the last processed Syncthing event id across restarts.
_SINCE_KEY = "memory_audit:syncthing:since"
# Long-poll timeout (seconds) the Syncthing events endpoint blocks for.
_POLL_TIMEOUT = 60
# Backoff after an error before retrying the poll loop.
_ERROR_BACKOFF = 15
_ROOT_HUB_NOTES = frozenset({"People.md", "Conversations.md", "Topics.md"})


def vault_sync_configured() -> bool:
    return bool(_SYNCTHING_API_KEY)


def _is_scaffold(item: str) -> bool:
    """Skip non-content notes: templates, bases, and top-level hub notes."""
    parts = item.split("/")
    if parts[0] in ("Templates",) or parts[0].startswith("."):
        return True
    if len(parts) == 1 and item in _ROOT_HUB_NOTES:
        return True
    return False


def _conversation_id_for(item: str) -> Optional[str]:
    if item.startswith("Conversations/") and item.endswith(".md"):
        return item[len("Conversations/") : -len(".md")]
    return None


async def _record_event(data: dict) -> None:
    """Translate one Syncthing ItemFinished event into an audit entry."""
    folder = data.get("folder", "")
    if not folder.startswith(_FOLDER_PREFIX):
        return
    if data.get("type") != "file":
        return
    item = data.get("item", "")
    if not item.endswith(".md") or _is_scaffold(item):
        return
    # A non-empty error means the pull did not actually apply — skip it.
    if data.get("error"):
        return

    user_id = folder[len(_FOLDER_PREFIX) :]
    action = data.get("action", "update")
    operation = "delete" if action == "delete" else "update"

    after: Optional[str] = None
    if operation != "delete":
        try:
            after = (_BACKEND_VAULTS_DIR / user_id / item).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 — file may be gone again already
            after = None

    with memory_provenance(MemoryCause.OBSIDIAN_SYNC):
        await record_vault_change(
            user_id=user_id,
            conversation_id=_conversation_id_for(item),
            operation=operation,
            note_path=item,
            after=after,
            agent_mode=False,
            summary=f"inbound Syncthing {action}",
        )


async def _initial_since(client: httpx.AsyncClient, redis) -> int:
    """Resume from the stored cursor, else start from the latest event (skip history)."""
    stored = await redis.get(_SINCE_KEY)
    if stored is not None:
        try:
            return int(stored)
        except (TypeError, ValueError):
            pass
    # No cursor yet: anchor at the most recent event so we don't replay history.
    try:
        resp = await client.get("/rest/events", params={"limit": 1})
        resp.raise_for_status()
        events = resp.json()
        return events[-1]["id"] if events else 0
    except Exception:  # noqa: BLE001
        return 0


async def _run_loop() -> None:
    redis = create_async_redis()
    try:
        while True:
            try:
                async with httpx.AsyncClient(
                    base_url=_SYNCTHING_URL,
                    headers={"X-API-Key": _SYNCTHING_API_KEY},
                    timeout=_POLL_TIMEOUT + 10,
                ) as client:
                    since = await _initial_since(client, redis)
                    logger.info(
                        "Syncthing memory-audit listener started (since=%s)", since
                    )
                    while True:
                        # Poll the UNFILTERED event stream and select ItemFinished
                        # client-side. Syncthing's `events=` type filter numbers
                        # events on a SEPARATE sequence that does not compose with
                        # the global `since` cursor, so a filtered poll never
                        # advances past our anchor.
                        resp = await client.get(
                            "/rest/events",
                            params={"since": since, "timeout": _POLL_TIMEOUT},
                        )
                        resp.raise_for_status()
                        events = resp.json()
                        for ev in events:
                            since = ev["id"]
                            if ev.get("type") != "ItemFinished":
                                continue
                            try:
                                await _record_event(ev.get("data", {}))
                            except Exception as e:  # noqa: BLE001
                                logger.warning("Failed to record inbound edit: %s", e)
                        if events:
                            await redis.set(_SINCE_KEY, since)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Syncthing memory-audit listener error: %s (retrying in %ss)",
                    e,
                    _ERROR_BACKOFF,
                )
                await asyncio.sleep(_ERROR_BACKOFF)
    finally:
        await redis.aclose()


def start_syncthing_audit_listener() -> Optional[asyncio.Task]:
    """Start the background listener if vault sync is configured; else no-op."""
    if not vault_sync_configured():
        logger.info(
            "Vault sync not configured (VAULT_SYNC_API_KEY unset) — "
            "inbound Obsidian edits will not be audited."
        )
        return None
    return asyncio.create_task(_run_loop(), name="syncthing-memory-audit")
