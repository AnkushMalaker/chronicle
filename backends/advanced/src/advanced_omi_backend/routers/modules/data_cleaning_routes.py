"""
Data-cleaning routes for Chronicle API.

Endpoints backing the Data Cleaning dashboard: batch audio analysis, filtered
listing with amplitude metrics + speaker labels, and audio archival (hard
delete of audio bytes, metadata kept).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.controllers import data_cleaning_controller
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data-cleaning", tags=["data-cleaning"])


class AnalyzeRequest(BaseModel):
    conversation_ids: Optional[List[str]] = Field(
        None, description="Subset to analyze; omit for all eligible conversations"
    )
    force: bool = Field(False, description="Re-analyze even if cached results exist")


class ArchiveRequest(BaseModel):
    conversation_ids: List[str] = Field(..., min_length=1)
    reason: str = Field(
        "manual_cleanup",
        description="Archive reason: near_silent, bad_speaker, manual_cleanup, etc.",
    )


@router.post("/analyze")
async def analyze(
    body: AnalyzeRequest,
    current_user: User = Depends(current_active_user),
):
    """Enqueue batch amplitude/silence analysis. Poll job status via /api/queue/jobs/{id}/status."""
    return await data_cleaning_controller.enqueue_analysis(
        current_user, body.conversation_ids, body.force
    )


@router.get("/conversations")
async def list_conversations(
    silence_threshold_dbfs: float = Query(
        -45.0, description="dBFS below which a window counts as silent"
    ),
    min_silent_fraction: float = Query(
        0.0, ge=0.0, le=1.0, description="Minimum silent fraction to include (0-1)"
    ),
    min_duration: float = Query(0.0, ge=0.0, description="Minimum duration in seconds"),
    include_speakers: Optional[str] = Query(
        None,
        description="Comma-separated speakers a conversation must contain at least one of",
    ),
    exclude_speakers: Optional[str] = Query(
        None, description="Comma-separated speakers a conversation must contain none of"
    ),
    archived_only: bool = Query(
        False,
        description="List archived metadata stubs instead of active conversations",
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(current_active_user),
):
    """List conversations with amplitude metrics + latest speaker labels, filtered."""

    def _csv(v: Optional[str]) -> Optional[list]:
        return [s.strip() for s in v.split(",") if s.strip()] if v else None

    return await data_cleaning_controller.list_for_cleaning(
        current_user,
        silence_threshold_dbfs=silence_threshold_dbfs,
        min_silent_fraction=min_silent_fraction,
        min_duration=min_duration,
        include_speakers=_csv(include_speakers),
        exclude_speakers=_csv(exclude_speakers),
        archived_only=archived_only,
        limit=limit,
        offset=offset,
    )


@router.get("/speakers")
async def list_speakers(current_user: User = Depends(current_active_user)):
    """Distinct latest-version speaker labels across the user's conversations."""
    return await data_cleaning_controller.list_speakers(current_user)


@router.post("/archive")
async def archive(
    body: ArchiveRequest,
    current_user: User = Depends(current_active_user),
):
    """Archive selected conversations: hard-delete audio bytes, keep metadata stub."""
    return await data_cleaning_controller.archive_audio_many(
        current_user, body.conversation_ids, body.reason
    )
