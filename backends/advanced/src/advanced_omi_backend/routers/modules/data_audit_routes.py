"""
Data-audit routes for Chronicle API.

Endpoints backing the Data Audit dashboard: batch VAD audio analysis,
filtered listing with speech metrics + speaker labels, audio archival (hard
delete of audio bytes, metadata kept), silence-gap detection, and
conversation split/merge.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from advanced_omi_backend.auth import (
    current_active_user,
    current_active_user_optional,
    get_user_from_token_param,
)
from advanced_omi_backend.controllers import data_audit_controller
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data-audit", tags=["data-audit"])


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


class SplitRequest(BaseModel):
    split_points: List[float] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Split points in seconds from conversation start",
    )


class MergeRequest(BaseModel):
    conversation_ids: List[str] = Field(
        ..., min_length=2, description="Adjacent conversations to merge, any order"
    )


class ScreenRequest(BaseModel):
    conversation_ids: List[str] = Field(..., min_length=1, max_length=200)
    policy: Optional[str] = Field(
        None,
        description="Shareability policy describing what's too sensitive to send "
        "to an annotator. Omit to use the configured default.",
    )


class ExportRequest(BaseModel):
    conversation_ids: List[str] = Field(..., min_length=1, max_length=200)
    mode: str = Field(
        "clips",
        pattern="^(clips|full)$",
        description="clips = one WAV per VAD speech region (silence cropped); "
        "full = one WAV per conversation, untouched",
    )
    pad_seconds: float = Field(
        1.0,
        ge=0.0,
        le=10.0,
        description="Padding added on both sides of each speech region",
    )
    speech_threshold: float = Field(
        0.5, ge=0.0, le=1.0, description="VAD frame probability counted as speech"
    )
    merge_gap_seconds: float = Field(
        3.0,
        ge=0.0,
        le=60.0,
        description="Speech regions closer than this become one clip",
    )
    excluded_ranges: Dict[str, List[List[float]]] = Field(
        default_factory=dict,
        description="conversation_id → withheld [start, end] time ranges (seconds) "
        "from the privacy screen; carved out of the exported audio + transcript",
    )
    sensitivity_policy: Optional[str] = Field(
        None, description="Policy used for the screen (recorded in export metadata)"
    )


@router.post("/analyze")
async def analyze(
    body: AnalyzeRequest,
    current_user: User = Depends(current_active_user),
):
    """Enqueue batch VAD analysis. Poll job status via /api/queue/jobs/{id}/status."""
    return await data_audit_controller.enqueue_analysis(
        current_user, body.conversation_ids, body.force
    )


@router.get("/conversations")
async def list_conversations(
    speech_threshold: float = Query(
        0.5,
        ge=0.0,
        le=1.0,
        description="VAD frame probability at/above which audio counts as speech",
    ),
    min_speech_fraction: float = Query(
        0.0,
        ge=0.0,
        le=1.0,
        description="Minimum speech fraction to include (0-1; 0 disables the filter)",
    ),
    max_speech_fraction: float = Query(
        1.0,
        ge=0.0,
        le=1.0,
        description="Maximum speech fraction to include (0-1; 1 disables the filter)",
    ),
    min_duration: float = Query(0.0, ge=0.0, description="Minimum duration in seconds"),
    max_duration: float = Query(
        0.0, ge=0.0, description="Maximum duration in seconds (0 disables the filter)"
    ),
    created_after: Optional[datetime] = Query(
        None, description="Only conversations created at/after this time (ISO 8601)"
    ),
    created_before: Optional[datetime] = Query(
        None, description="Only conversations created at/before this time (ISO 8601)"
    ),
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
    hide_failed: bool = Query(
        False,
        description="Exclude conversations the pipeline marked processing_status='failed'",
    ),
    hide_reviewed: bool = Query(
        False,
        description="Exclude conversations with nothing left to review (no unidentified speech segments)",
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(current_active_user),
):
    """List conversations with VAD speech metrics + latest speaker labels, filtered."""

    def _csv(v: Optional[str]) -> Optional[list]:
        return [s.strip() for s in v.split(",") if s.strip()] if v else None

    return await data_audit_controller.list_for_audit(
        current_user,
        speech_threshold=speech_threshold,
        min_speech_fraction=min_speech_fraction,
        max_speech_fraction=max_speech_fraction,
        min_duration=min_duration,
        max_duration=max_duration,
        created_after=created_after,
        created_before=created_before,
        include_speakers=_csv(include_speakers),
        exclude_speakers=_csv(exclude_speakers),
        archived_only=archived_only,
        hide_failed=hide_failed,
        hide_reviewed=hide_reviewed,
        limit=limit,
        offset=offset,
    )


@router.post("/archive")
async def archive(
    body: ArchiveRequest,
    current_user: User = Depends(current_active_user),
):
    """Archive selected conversations: hard-delete audio bytes, keep metadata stub."""
    return await data_audit_controller.archive_audio_many(
        current_user, body.conversation_ids, body.reason
    )


@router.get("/conversations/{conversation_id}/silence-gaps")
async def silence_gaps(
    conversation_id: str,
    speech_threshold: float = Query(
        0.5,
        ge=0.0,
        le=1.0,
        description="Chunk max VAD probability below which the chunk counts as silent",
    ),
    min_gap_seconds: float = Query(
        900.0, gt=0.0, description="Minimum silence-gap length to report"
    ),
    current_user: User = Depends(current_active_user),
):
    """Detected long silence gaps (candidate split points) from cached chunk VAD scores."""
    return await data_audit_controller.get_silence_gaps(
        current_user,
        conversation_id,
        speech_threshold=speech_threshold,
        min_gap_seconds=min_gap_seconds,
    )


@router.get("/conversations/{conversation_id}/speech-regions")
async def speech_regions(
    conversation_id: str,
    speakers: Optional[str] = Query(
        None,
        description="Comma-separated speaker labels; regions are limited to "
        "VAD speech overlapping those speakers' transcript segments",
    ),
    current_user: User = Depends(current_active_user),
):
    """Merged speech intervals (for speech-skip playback), derived from cached VAD scores."""
    speaker_list = (
        [s.strip() for s in speakers.split(",") if s.strip()] if speakers else None
    )
    return await data_audit_controller.get_speech_regions(
        current_user, conversation_id, speakers=speaker_list
    )


@router.get("/conversations/{conversation_id}/segments")
async def list_segments(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Active-version transcript segments (with speaker-recognition confidence)
    for the speaker-triage panel."""
    return await data_audit_controller.get_segments(current_user, conversation_id)


class IdentifySegmentRequest(BaseModel):
    start: float = Field(..., ge=0.0, description="Segment start time in seconds")
    end: float = Field(..., gt=0.0, description="Segment end time in seconds")


@router.post("/conversations/{conversation_id}/segments/identify")
async def identify_segment(
    conversation_id: str,
    body: IdentifySegmentRequest,
    current_user: User = Depends(current_active_user),
):
    """Live speaker suggestion for one segment: returns the closest enrolled
    speaker + cosine for the clip (even below the match threshold)."""
    return await data_audit_controller.identify_segment_clip(
        current_user, conversation_id, body.start, body.end
    )


@router.get("/speakers/confidence")
async def speakers_confidence(current_user: User = Depends(current_active_user)):
    """Per-speaker identification-confidence stats across the corpus: distribution
    histogram, marginal-match fraction, per-speaker baselines (mean/median/
    %marginal), threshold survival counts, and a data-driven recommended
    similarity threshold. Computed from stored confidence — no re-embedding."""
    return await data_audit_controller.speaker_confidence_overview(current_user)


@router.get("/triage/pending")
async def triage_pending(current_user: User = Depends(current_active_user)):
    """Count of unapplied speaker-triage decisions and conversations they span."""
    return await data_audit_controller.get_triage_pending(current_user)


@router.post("/triage/apply")
async def apply_triage(current_user: User = Depends(current_active_user)):
    """Bulk-apply all pending speaker-triage decisions across every conversation:
    new transcript versions with corrected labels, voiceprint enrollment, and
    chained memory reprocessing (noise decisions skip enrollment)."""
    return await data_audit_controller.apply_triage(current_user)


@router.post("/conversations/{conversation_id}/split")
async def split(
    conversation_id: str,
    body: SplitRequest,
    current_user: User = Depends(current_active_user),
):
    """Split a conversation into children at the given time points.

    Audio chunks are reassigned (no re-encode), the transcript is sliced by
    time range, the parent is soft-deleted with lineage metadata, and memory +
    title jobs are enqueued for each child with a transcript.
    """
    return await data_audit_controller.split_conversation(
        current_user, conversation_id, body.split_points
    )


@router.get("/sensitivity-policy")
async def sensitivity_policy(current_user: User = Depends(current_active_user)):
    """The configured default shareability policy (prefill for the screen UI)."""
    return await data_audit_controller.get_default_sensitivity_policy()


@router.post("/export/screen")
async def screen_export(
    body: ScreenRequest,
    current_user: User = Depends(current_active_user),
):
    """Enqueue a privacy screen over the selected conversations.

    Applies the shareability policy to each transcript and flags segments too
    personal to share with an annotator. Poll job status via
    /api/queue/jobs/{id}/status; the result lists flagged segments (with time
    ranges) to review, then pass the confirmed ranges to /export as
    ``excluded_ranges``.
    """
    return await data_audit_controller.start_screening(
        current_user, body.conversation_ids, body.policy
    )


@router.post("/export")
async def start_export(
    body: ExportRequest,
    current_user: User = Depends(current_active_user),
):
    """Enqueue an annotation-dataset export: WAV audio + transcript manifest,
    zipped for download. Mode ``clips`` cuts one padded WAV per VAD speech
    region (silence cropped); mode ``full`` exports each conversation as a
    single untouched WAV. ``excluded_ranges`` from the privacy screen are
    carved out of the exported audio + transcript.

    Poll job status via /api/queue/jobs/{id}/status, then download from
    /api/data-audit/exports/{export_id}/download.
    """
    return await data_audit_controller.start_export(
        current_user,
        body.conversation_ids,
        mode=body.mode,
        pad_seconds=body.pad_seconds,
        speech_threshold=body.speech_threshold,
        merge_gap_seconds=body.merge_gap_seconds,
        excluded_ranges=body.excluded_ranges,
        sensitivity_policy=body.sensitivity_policy,
    )


@router.get("/exports")
async def list_exports(current_user: User = Depends(current_active_user)):
    """List completed annotation exports with their summaries."""
    return await data_audit_controller.list_exports(current_user)


@router.get("/exports/{export_id}/download")
async def download_export(
    export_id: str,
    token: Optional[str] = Query(
        default=None, description="JWT token for direct browser download links"
    ),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """Download an export's dataset.zip (header auth or ?token= for <a href>)."""
    if not current_user and token:
        current_user = await get_user_from_token_param(token)
    if not current_user:
        return JSONResponse(
            status_code=401, content={"error": "Authentication required"}
        )
    return await data_audit_controller.download_export(current_user, export_id)


@router.delete("/exports/{export_id}")
async def delete_export(
    export_id: str,
    current_user: User = Depends(current_active_user),
):
    """Delete an export (zip + metadata) from disk."""
    return await data_audit_controller.delete_export(current_user, export_id)


@router.post("/merge")
async def merge(
    body: MergeRequest,
    current_user: User = Depends(current_active_user),
):
    """Merge adjacent conversations (same client, consecutive in time) into a new one.

    Audio chunks are reassigned with time offsets, transcripts are concatenated
    with a seam note where wall-clock gaps are elided, sources are soft-deleted
    with lineage metadata, and memory + title jobs are enqueued.
    """
    return await data_audit_controller.merge_conversations(
        current_user, body.conversation_ids
    )
