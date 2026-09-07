"""Authenticated push and retrieval endpoints for mobile diagnostic logs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from backend.auth import current_active_user
from backend.services.client_diagnostics import (
    MAX_CLIENT_DIAGNOSTIC_BYTES,
    list_client_diagnostics,
    read_client_diagnostic,
    store_client_diagnostic,
)
from backend.users import User

router = APIRouter(prefix="/client-diagnostics", tags=["client-diagnostics"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_client_diagnostic(
    request: Request,
    current_user: User = Depends(current_active_user),
    platform: str | None = Header(default=None, alias="X-Chronicle-Platform"),
    app_version: str | None = Header(default=None, alias="X-Chronicle-App-Version"),
    build_version: str | None = Header(default=None, alias="X-Chronicle-Build-Version"),
    device_id: str | None = Header(default=None, alias="X-Chronicle-Device-ID"),
):
    """Accept a bounded UTF-8 device log pushed by the authenticated phone."""

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_CLIENT_DIAGNOSTIC_BYTES:
                raise HTTPException(
                    status_code=413, detail="Diagnostic log is too large."
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length."
            ) from exc

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_CLIENT_DIAGNOSTIC_BYTES:
            raise HTTPException(status_code=413, detail="Diagnostic log is too large.")

    try:
        receipt = await store_client_diagnostic(
            user_id=current_user.user_id,
            content=bytes(body),
            platform=platform,
            app_version=app_version,
            build_version=build_version,
            device_id=device_id,
        )
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="Diagnostic log must be UTF-8 text."
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return receipt


@router.get("")
async def get_client_diagnostic_receipts(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(current_active_user),
):
    """List the authenticated user's recent upload receipts."""

    return {"items": await list_client_diagnostics(current_user.user_id, limit=limit)}


@router.get("/{upload_id}", response_class=PlainTextResponse)
async def get_client_diagnostic(
    upload_id: str,
    current_user: User = Depends(current_active_user),
):
    """Download one log owned by the authenticated user."""

    try:
        return await read_client_diagnostic(current_user.user_id, upload_id)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=404, detail="Diagnostic log not found.")
