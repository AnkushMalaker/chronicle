"""Controller for the system-event ledger (admin "System Errors" page).

Read/manage side of the store written by
:mod:`advanced_omi_backend.services.observability`. All callers are admin-gated at
the route layer.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from beanie import PydanticObjectId
from fastapi import HTTPException

logger = logging.getLogger(__name__)


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
    }


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
    from advanced_omi_backend.models.system_event import SystemEvent

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
    """Counts by severity/category/source over a time window (for the strip + badge)."""
    from advanced_omi_backend.models.system_event import SystemEvent

    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
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
    cursor = SystemEvent.get_pymongo_collection().aggregate(pipeline)
    rows = await cursor.to_list(length=1)
    facet = rows[0] if rows else {}

    def _kv(items: list) -> dict[str, int]:
        return {i["_id"]: i["count"] for i in items if i.get("_id") is not None}

    def _n(items: list) -> int:
        return items[0]["n"] if items else 0

    return {
        "window_hours": window_hours,
        "total": _n(facet.get("total", [])),
        "unacked": _n(facet.get("unacked", [])),
        "by_severity": _kv(facet.get("by_severity", [])),
        "by_category": _kv(facet.get("by_category", [])),
        "by_source": _kv(facet.get("by_source", [])),
    }


async def ack_system_event(event_id: str) -> dict[str, Any]:
    from advanced_omi_backend.models.system_event import SystemEvent

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
    from advanced_omi_backend.models.system_event import SystemEvent

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
    from advanced_omi_backend.models.system_event import SystemEvent

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
    from advanced_omi_backend.models.system_event import SystemEvent

    if acked_only:
        result = await SystemEvent.find(
            SystemEvent.acked == True
        ).delete()  # noqa: E712
    else:
        result = await SystemEvent.find_all().delete()
    return {"success": True, "deleted": getattr(result, "deleted_count", None)}
