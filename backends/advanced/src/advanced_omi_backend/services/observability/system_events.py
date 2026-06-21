"""Recorder, live publisher, and ingest drain for the system-event ledger.

Two recording entry points:

* :func:`record_event` — ``async``; used by FastAPI-process code (the health
  poller). Persists to Mongo and publishes SSE inline.
* :func:`record_event_sync` — sync; used by RQ workers and the logging handler and
  by every semantic tap. It does **one** Redis ``LPUSH`` onto an ingest list and
  nothing else — no Mongo, no event loop. A single async drain
  (:func:`run_event_ingest_drain`) running in the FastAPI process pops the list and
  persists. This keeps all the sync/worker paths free of async-Mongo coupling.

Recording is always best-effort: a failure here must never break the caller.
Recurring identical events collapse onto one row (see ``_DEDUP_WINDOW_SECS``).

Modeled on :func:`advanced_omi_backend.services.memory.audit.record_vault_change`
and :func:`advanced_omi_backend.services.sse_publisher.publish_sse_event`.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from advanced_omi_backend.redis_factory import create_async_redis, create_sync_redis

logger = logging.getLogger("observability.system_events")

# Redis list the sync side pushes onto; the async drain pops it.
INGEST_KEY = "system:events:ingest"
# Cap pending events so a long backend outage can't grow the list unbounded.
_INGEST_MAX = 10_000
# Collapse identical recurring events within this window onto one row.
_DEDUP_WINDOW_SECS = 30
# How long admin-id lookups for SSE fan-out are cached.
_ADMIN_CACHE_TTL = 60

_sync_redis = None
_admin_ids_cache: dict[str, Any] = {"ids": [], "ts": 0.0}


def _get_sync_redis():
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = create_sync_redis(decode_responses=True)
    return _sync_redis


def _fingerprint(severity: str, category: str, source: str, title: str) -> str:
    raw = f"{severity}|{category}|{source}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _build(
    *,
    severity: str,
    category: str,
    source: str,
    title: str,
    detail: Optional[str],
    traceback: Optional[str],
    user_id: Optional[str],
    client_id: Optional[str],
    conversation_id: Optional[str],
    metadata: Optional[dict],
) -> dict:
    return {
        "severity": severity,
        "category": category,
        "source": (source or "unknown")[:200],
        "title": (title or "")[:500],
        "detail": detail,
        "traceback": traceback,
        "user_id": user_id,
        "client_id": client_id,
        "conversation_id": conversation_id,
        "metadata": metadata or {},
        "fingerprint": _fingerprint(
            severity, category, source or "unknown", title or ""
        ),
    }


async def record_event(
    *,
    severity: str,
    category: str,
    source: str,
    title: str,
    detail: Optional[str] = None,
    traceback: Optional[str] = None,
    user_id: Optional[str] = None,
    client_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Record an event from async (FastAPI-process) code: persist + publish inline."""
    event = _build(
        severity=severity,
        category=category,
        source=source,
        title=title,
        detail=detail,
        traceback=traceback,
        user_id=user_id,
        client_id=client_id,
        conversation_id=conversation_id,
        metadata=metadata,
    )
    await _persist_and_publish(event)


def record_event_sync(
    *,
    severity: str,
    category: str,
    source: str,
    title: str,
    detail: Optional[str] = None,
    traceback: Optional[str] = None,
    user_id: Optional[str] = None,
    client_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Record an event from sync code (workers, logging handler, taps): enqueue only."""
    try:
        event = _build(
            severity=severity,
            category=category,
            source=source,
            title=title,
            detail=detail,
            traceback=traceback,
            user_id=user_id,
            client_id=client_id,
            conversation_id=conversation_id,
            metadata=metadata,
        )
        r = _get_sync_redis()
        r.lpush(INGEST_KEY, json.dumps(event))
        r.ltrim(INGEST_KEY, 0, _INGEST_MAX - 1)
    except Exception:  # noqa: BLE001 — recording must never break the caller
        pass


async def _persist_and_publish(event: dict) -> None:
    try:
        doc = await _upsert(event)
        if doc is not None:
            await _fan_out_sse(doc)
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("Failed to record system event %r: %s", event.get("title"), e)


async def _upsert(event: dict):
    """Insert a new event, or collapse onto a recent identical one (dedup window)."""
    from advanced_omi_backend.models.system_event import SystemEvent

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=_DEDUP_WINDOW_SECS)
    existing = await SystemEvent.find_one(
        SystemEvent.fingerprint == event["fingerprint"],
        SystemEvent.last_seen_at >= window_start,
    )
    if existing is not None:
        existing.occurrences += 1
        existing.last_seen_at = now
        if event.get("detail"):
            existing.detail = event["detail"]
        if event.get("traceback"):
            existing.traceback = event["traceback"]
        await existing.save()
        return existing

    doc = SystemEvent(
        severity=event["severity"],
        category=event["category"],
        source=event["source"],
        title=event["title"],
        detail=event.get("detail"),
        traceback=event.get("traceback"),
        user_id=event.get("user_id"),
        client_id=event.get("client_id"),
        conversation_id=event.get("conversation_id"),
        fingerprint=event["fingerprint"],
        metadata=event.get("metadata") or {},
        created_at=now,
        last_seen_at=now,
    )
    await doc.insert()
    return doc


async def _get_admin_ids() -> list[str]:
    now = time.time()
    if _admin_ids_cache["ids"] and now - _admin_ids_cache["ts"] < _ADMIN_CACHE_TTL:
        return _admin_ids_cache["ids"]
    try:
        from advanced_omi_backend.models.user import User

        admins = await User.find(User.is_superuser == True).to_list()  # noqa: E712
        _admin_ids_cache["ids"] = [str(u.id) for u in admins]
        _admin_ids_cache["ts"] = now
    except Exception:  # noqa: BLE001
        logger.debug("Failed to refresh admin ids for SSE fan-out", exc_info=True)
    return _admin_ids_cache["ids"]


async def _fan_out_sse(doc) -> None:
    """Push a lightweight notification to every admin's SSE channel (per-user bus)."""
    try:
        from advanced_omi_backend.services.sse_publisher import publish_sse_event

        payload = {
            "id": str(doc.id),
            "severity": doc.severity,
            "category": doc.category,
            "source": doc.source,
            "title": doc.title,
        }
        for admin_id in await _get_admin_ids():
            publish_sse_event(admin_id, "system.error", payload)
    except Exception:  # noqa: BLE001
        logger.debug("SSE fan-out for system event failed", exc_info=True)


async def run_event_ingest_drain() -> None:
    """Drain the sync ingest list into Mongo + SSE. Runs in the FastAPI process."""
    redis = create_async_redis(decode_responses=True)
    logger.info("📋 System-event ingest drain started")
    try:
        while True:
            try:
                item = await redis.brpop([INGEST_KEY], timeout=5)
                if item is None:
                    continue
                _key, raw = item
                await _persist_and_publish(json.loads(raw))
            except asyncio.CancelledError:
                logger.info("📋 System-event ingest drain stopped")
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("System-event drain iteration failed: %s", e)
                await asyncio.sleep(1)
    finally:
        await redis.aclose()
