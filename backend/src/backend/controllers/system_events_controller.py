"""Controller for the system-event ledger (admin "System Errors" page).

Read/manage side of the store written by
:mod:`backend.services.observability`. All callers are admin-gated at
the route layer.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from beanie import PydanticObjectId
from fastapi import HTTPException

from backend.models.system_event import SystemEvent
from backend.services.observability.system_events import record_event

logger = logging.getLogger(__name__)

_DETERMINISTIC_MEMORY_FALLBACK = "deterministic_source_preserving_note"


def _system_event_to_dict(doc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "severity": doc.severity,
        "category": doc.category,
        "source": doc.source,
        "title": doc.title,
        "detail": doc.detail,
        "traceback": doc.traceback,
        "user_id": doc.user_id,
        "client_id": doc.client_id,
        "conversation_id": doc.conversation_id,
        "count": doc.occurrences,
        "acked": doc.acked,
        "metadata": doc.metadata,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "last_seen_at": doc.last_seen_at.isoformat() if doc.last_seen_at else None,
        "occurrence_times": [
            occurred_at.isoformat() for occurred_at in doc.occurrence_times
        ],
    }


# Bounds for externally-submitted event fields, so a misbehaving service can't
# push a multi-megabyte traceback into the ledger. (source/title are additionally
# clamped by the recorder's _build to 200/500 chars.)
_MAX_DETAIL = 4000
_MAX_TRACEBACK = 20000
_VALID_SEVERITIES = ("info", "warning", "error", "critical")


async def ingest_external_event(
    *,
    severity: str,
    category: Optional[str],
    source: str,
    title: str,
    detail: Optional[str],
    traceback: Optional[str],
    client_id: Optional[str],
    conversation_id: Optional[str],
    metadata: Optional[dict],
    incident_key: Optional[str] = None,
    resolves_incident: bool = False,
) -> dict[str, Any]:
    """Record an event submitted over HTTP by another service (token-gated route).

    Treats the payload as untrusted input: severity is constrained to the known
    set, free-text fields are size-clamped, and the source is namespaced with a
    ``service:`` prefix so a remote service can't masquerade as a backend logger.
    """
    severity = severity if severity in _VALID_SEVERITIES else "error"
    # External submissions are, by definition, service-originated unless they say
    # otherwise; default the category accordingly.
    category = (category or "service")[:50]
    # Namespace the source so it's unambiguous in the feed and can't collide with
    # an internal logger name. The shared token authenticates "a trusted node",
    # not a specific identity, so we don't trust the raw name beyond labelling.
    source = f"service:{(source or 'unknown').strip()[:180]}"
    title = (title or "(no title)").strip()
    if detail and len(detail) > _MAX_DETAIL:
        detail = detail[:_MAX_DETAIL] + "… (truncated)"
    if traceback and len(traceback) > _MAX_TRACEBACK:
        traceback = traceback[:_MAX_TRACEBACK] + "\n… (truncated)"

    await record_event(
        severity=severity,
        category=category,
        source=source,
        title=title,
        detail=detail,
        traceback=traceback,
        client_id=client_id,
        conversation_id=conversation_id,
        metadata=metadata or {},
        # Namespaced for the same reason as source: the shared token authenticates
        # "a trusted node", so an external key must not be able to close an
        # incident opened by internal code.
        incident_key=f"service:{incident_key[:180]}" if incident_key else None,
        resolves_incident=bool(resolves_incident),
    )
    return {"status": "recorded"}


async def list_system_events(
    *,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    client_id: Optional[str] = None,
    user_id: Optional[str] = None,
    acked: Optional[bool] = None,
    since_hours: Optional[float] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated, newest-first list of events with optional facet filters."""
    conditions = []
    if severity:
        conditions.append(SystemEvent.severity == severity)
    if category:
        conditions.append(SystemEvent.category == category)
    if source:
        conditions.append(SystemEvent.source == source)
    if client_id:
        conditions.append(SystemEvent.client_id == client_id)
    if user_id:
        conditions.append(SystemEvent.user_id == user_id)
    if acked is not None:
        conditions.append(SystemEvent.acked == acked)
    if since_hours:
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        conditions.append(SystemEvent.created_at >= since)

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    total = await SystemEvent.find(*conditions).count()
    docs = (
        await SystemEvent.find(*conditions)
        .sort("-created_at")
        .skip(offset)
        .limit(limit)
        .to_list()
    )
    return {
        "events": [_system_event_to_dict(d) for d in docs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def get_system_events_summary(*, window_hours: float = 24) -> dict[str, Any]:
    """Counts and memory-fallback statistics over an operational time window."""
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    collection = SystemEvent.get_pymongo_collection()
    pipeline = [
        {"$match": {"created_at": {"$gte": since}}},
        {
            "$facet": {
                "by_severity": [{"$group": {"_id": "$severity", "count": {"$sum": 1}}}],
                "by_category": [{"$group": {"_id": "$category", "count": {"$sum": 1}}}],
                "by_source": [
                    {"$group": {"_id": "$source", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": 15},
                ],
                "total": [{"$count": "n"}],
                "unacked": [
                    {"$match": {"acked": False}},
                    {"$count": "n"},
                ],
            }
        },
    ]
    cursor = collection.aggregate(pipeline)
    rows = await cursor.to_list(length=1)
    facet = rows[0] if rows else {}

    # One ledger row may represent several observations because the recorder
    # de-duplicates rapid repeats. Unwind the recorded timestamps so this is an
    # occurrence count for the selected window, not a document count. Matching the
    # timestamps also includes a retry in the window when its row was created earlier.
    fallback_pipeline = [
        {
            "$match": {
                "category": "memory",
                "metadata.fallback_type": _DETERMINISTIC_MEMORY_FALLBACK,
                "occurrence_times": {"$gte": since},
            }
        },
        {"$unwind": "$occurrence_times"},
        {"$match": {"occurrence_times": {"$gte": since}}},
        {
            "$facet": {
                "total": [{"$count": "n"}],
                "affected_conversations": [
                    {"$match": {"conversation_id": {"$ne": None}}},
                    {"$group": {"_id": "$conversation_id"}},
                    {"$count": "n"},
                ],
                "by_reason": [
                    {"$unwind": "$metadata.reasons"},
                    {
                        "$group": {
                            "_id": "$metadata.reasons",
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"count": -1}},
                ],
                "by_user": [
                    {"$match": {"user_id": {"$ne": None}}},
                    {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ],
                "by_primary_backend": [
                    {
                        "$group": {
                            "_id": "$metadata.primary_backend",
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"count": -1}},
                ],
                "by_recovery_backend": [
                    {
                        "$group": {
                            "_id": "$metadata.recovery_backend",
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"count": -1}},
                ],
                "by_agent_path": [
                    {
                        "$match": {
                            "metadata.primary_backend": {"$ne": None},
                            "metadata.recovery_backend": {"$ne": None},
                        }
                    },
                    {
                        "$group": {
                            "_id": {
                                "primary": "$metadata.primary_backend",
                                "recovery": "$metadata.recovery_backend",
                            },
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"count": -1}},
                ],
                "latest": [
                    {"$sort": {"occurrence_times": -1}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "at": "$occurrence_times"}},
                ],
            }
        },
    ]
    fallback_cursor = collection.aggregate(fallback_pipeline)
    fallback_rows = await fallback_cursor.to_list(length=1)
    fallback_facet = fallback_rows[0] if fallback_rows else {}

    def _kv(items: list) -> dict[str, int]:
        return {i["_id"]: i["count"] for i in items if i.get("_id") is not None}

    def _n(items: list) -> int:
        return items[0]["n"] if items else 0

    def _agent_paths(items: list) -> list[dict[str, Any]]:
        paths = []
        for item in items:
            path = item.get("_id") or {}
            primary = path.get("primary")
            recovery = path.get("recovery")
            if primary is None or recovery is None:
                continue
            paths.append(
                {
                    "primary_backend": primary,
                    "recovery_backend": recovery,
                    "occurrences": item["count"],
                }
            )
        return paths

    latest_items = fallback_facet.get("latest", [])
    latest_at = latest_items[0].get("at") if latest_items else None
    if isinstance(latest_at, datetime):
        if latest_at.tzinfo is None:
            latest_at = latest_at.replace(tzinfo=timezone.utc)
        else:
            latest_at = latest_at.astimezone(timezone.utc)
        latest_at = latest_at.isoformat()

    return {
        "window_hours": window_hours,
        "total": _n(facet.get("total", [])),
        "unacked": _n(facet.get("unacked", [])),
        "by_severity": _kv(facet.get("by_severity", [])),
        "by_category": _kv(facet.get("by_category", [])),
        "by_source": _kv(facet.get("by_source", [])),
        "memory_fallbacks": {
            "occurrences": _n(fallback_facet.get("total", [])),
            "affected_conversations": _n(
                fallback_facet.get("affected_conversations", [])
            ),
            "by_reason": _kv(fallback_facet.get("by_reason", [])),
            "by_user": _kv(fallback_facet.get("by_user", [])),
            "by_primary_backend": _kv(fallback_facet.get("by_primary_backend", [])),
            "by_recovery_backend": _kv(fallback_facet.get("by_recovery_backend", [])),
            "agent_paths": _agent_paths(fallback_facet.get("by_agent_path", [])),
            "latest_at": latest_at,
        },
    }


async def ack_system_event(event_id: str) -> dict[str, Any]:
    try:
        oid = PydanticObjectId(event_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid event id")
    doc = await SystemEvent.get(oid)
    if doc is None:
        raise HTTPException(status_code=404, detail="Event not found")
    doc.acked = True
    await doc.save()
    return {"success": True, "id": event_id}


async def ack_system_events_by_ids(event_ids: list[str]) -> dict[str, Any]:
    """Acknowledge a specific set of events by id (skips any already acked)."""
    oids = []
    for eid in event_ids:
        try:
            oids.append(PydanticObjectId(eid))
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid event id: {eid}")
    if not oids:
        return {"success": True, "acked": 0}

    result = await SystemEvent.find(
        {"_id": {"$in": oids}, "acked": False},
    ).update({"$set": {"acked": True}})
    return {"success": True, "acked": getattr(result, "modified_count", None)}


async def ack_system_events(
    *,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    client_id: Optional[str] = None,
    user_id: Optional[str] = None,
    since_hours: Optional[float] = None,
) -> dict[str, Any]:
    """Acknowledge all currently-unacked events matching the given filters.

    Filters mirror :func:`list_system_events` so the UI can acknowledge exactly the
    set the operator is currently looking at. Always scoped to ``acked == False``.
    """
    conditions = [SystemEvent.acked == False]  # noqa: E712
    if severity:
        conditions.append(SystemEvent.severity == severity)
    if category:
        conditions.append(SystemEvent.category == category)
    if source:
        conditions.append(SystemEvent.source == source)
    if client_id:
        conditions.append(SystemEvent.client_id == client_id)
    if user_id:
        conditions.append(SystemEvent.user_id == user_id)
    if since_hours:
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        conditions.append(SystemEvent.created_at >= since)

    result = await SystemEvent.find(*conditions).update({"$set": {"acked": True}})
    return {"success": True, "acked": getattr(result, "modified_count", None)}


async def clear_system_events(*, acked_only: bool = False) -> dict[str, Any]:
    if acked_only:
        result = await SystemEvent.find(
            SystemEvent.acked == True
        ).delete()  # noqa: E712
    else:
        result = await SystemEvent.find_all().delete()
    return {"success": True, "deleted": getattr(result, "deleted_count", None)}
