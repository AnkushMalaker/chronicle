"""System-event ledger routes (admin-only "System Errors" page).

Read/manage the central operational+application error store. All endpoints require
a superuser. Live updates are delivered separately over the SSE bus
(``system.error`` events), so these are plain request/response.
"""

import hmac
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from backend.auth import current_superuser
from backend.controllers import system_events_controller
from backend.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/system-events", tags=["system-events"])


def _ingest_token() -> str:
    """Shared secret other services present to push events. Falls back to the
    node-agent token so single/distributed setups that already have
    SERVICE_MANAGER_TOKEN work without configuring a second secret."""
    return (
        os.getenv("SYSTEM_EVENT_INGEST_TOKEN")
        or os.getenv("SERVICE_MANAGER_TOKEN")
        or ""
    )


async def verify_ingest_token(authorization: str = Header(default="")) -> None:
    """Bearer-token gate for the cross-service ingest endpoint. Unlike the rest of
    this router (superuser-gated), services authenticate with a shared token, not a
    user session."""
    expected = _ingest_token()
    if not expected:
        # No token configured → endpoint is closed, not open.
        raise HTTPException(
            status_code=503,
            detail="System-event ingest is not enabled (no SYSTEM_EVENT_INGEST_TOKEN / SERVICE_MANAGER_TOKEN).",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid ingest token.")


class AckSelectedRequest(BaseModel):
    event_ids: list[str]


class IngestEventRequest(BaseModel):
    severity: str = "error"
    category: Optional[str] = None
    source: str
    title: str
    detail: Optional[str] = None
    traceback: Optional[str] = None
    client_id: Optional[str] = None
    conversation_id: Optional[str] = None
    metadata: Optional[dict] = None
    # Lets a service report a recurring fault as one incident: a repeat bumps
    # occurrences instead of adding a row, and it can close the incident itself
    # once the fault clears. Used by the node agent's host watchdog, which polls
    # on a timer and would otherwise append an identical event every cycle.
    incident_key: Optional[str] = None
    resolves_incident: bool = False


@router.post("/ingest", dependencies=[Depends(verify_ingest_token)])
async def ingest_system_event(body: IngestEventRequest):
    """Record an event submitted by another Chronicle service (ASR, speaker-rec,
    …). Token-gated, not user-gated, so services can report without a login."""
    return await system_events_controller.ingest_external_event(
        severity=body.severity,
        category=body.category,
        source=body.source,
        title=body.title,
        detail=body.detail,
        traceback=body.traceback,
        client_id=body.client_id,
        conversation_id=body.conversation_id,
        metadata=body.metadata,
        incident_key=body.incident_key,
        resolves_incident=body.resolves_incident,
    )


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
