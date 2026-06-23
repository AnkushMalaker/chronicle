"""System-event ledger routes (admin-only "System Errors" page).

Read/manage the central operational+application error store. All endpoints require
a superuser. Live updates are delivered separately over the SSE bus
(``system.error`` events), so these are plain request/response.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from advanced_omi_backend.auth import current_superuser
from advanced_omi_backend.controllers import system_events_controller
from advanced_omi_backend.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/system-events", tags=["system-events"])


class AckSelectedRequest(BaseModel):
    event_ids: list[str]


@router.get("")
async def list_system_events(
    severity: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    client_id: Optional[str] = None,
    user_id: Optional[str] = None,
    acked: Optional[bool] = None,
    since_hours: Optional[float] = None,
    limit: int = 100,
    offset: int = 0,
    current_user: User = Depends(current_superuser),
):
    """List system events (newest first) with optional filters. Admin only."""
    return await system_events_controller.list_system_events(
        severity=severity,
        category=category,
        source=source,
        client_id=client_id,
        user_id=user_id,
        acked=acked,
        since_hours=since_hours,
        limit=limit,
        offset=offset,
    )


@router.get("/summary")
async def system_events_summary(
    window_hours: float = 24,
    current_user: User = Depends(current_superuser),
):
    """Counts by severity/category/source over a window. Admin only."""
    return await system_events_controller.get_system_events_summary(
        window_hours=window_hours
    )


@router.post("/{event_id}/ack")
async def ack_system_event(
    event_id: str,
    current_user: User = Depends(current_superuser),
):
    """Acknowledge a single event. Admin only."""
    return await system_events_controller.ack_system_event(event_id)


@router.post("/ack-selected")
async def ack_selected_system_events(
    body: AckSelectedRequest,
    current_user: User = Depends(current_superuser),
):
    """Acknowledge a specific set of events by id. Admin only."""
    return await system_events_controller.ack_system_events_by_ids(body.event_ids)


@router.post("/ack-all")
async def ack_all_system_events(
    severity: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    client_id: Optional[str] = None,
    user_id: Optional[str] = None,
    since_hours: Optional[float] = None,
    current_user: User = Depends(current_superuser),
):
    """Acknowledge all unacked events matching the given filters. Admin only."""
    return await system_events_controller.ack_system_events(
        severity=severity,
        category=category,
        source=source,
        client_id=client_id,
        user_id=user_id,
        since_hours=since_hours,
    )


@router.post("/clear")
async def clear_system_events(
    acked_only: bool = False,
    current_user: User = Depends(current_superuser),
):
    """Delete events (all, or only acknowledged ones). Admin only."""
    return await system_events_controller.clear_system_events(acked_only=acked_only)
