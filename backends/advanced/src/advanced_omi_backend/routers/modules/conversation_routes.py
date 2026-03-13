"""
Conversation management routes for Chronicle API.

Handles conversation CRUD operations, audio processing, and transcript management.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.controllers import conversation_controller
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.users import User
from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_audio_segment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("/{client_id}/close")
async def close_current_conversation(
    client_id: str,
    current_user: User = Depends(current_active_user),
):
    """Close the current active conversation for a client. Works for both connected and disconnected clients."""
    return await conversation_controller.close_current_conversation(
        client_id, current_user
    )


@router.get("")
async def get_conversations(
    include_deleted: bool = Query(
        False, description="Include soft-deleted conversations"
    ),
    include_unprocessed: bool = Query(
        False,
        description="Include orphan audio sessions (always_persist with failed/pending transcription)",
    ),
    starred_only: bool = Query(
        False, description="Only return starred/favorited conversations"
    ),
    limit: int = Query(200, ge=1, le=500, description="Max conversations to return"),
    offset: int = Query(0, ge=0, description="Number of conversations to skip"),
    sort_by: str = Query(
        "created_at", description="Sort field: created_at, title, audio_total_duration"
    ),
    sort_order: str = Query("desc", description="Sort direction: asc or desc"),
    current_user: User = Depends(current_active_user),
):
    """Get conversations. Admins see all conversations, users see only their own."""
    return await conversation_controller.get_conversations(
        current_user,
        include_deleted,
        include_unprocessed,
        starred_only,
        limit,
        offset,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.get("/search")
async def search_conversations(
    q: str = Query(..., min_length=1, description="Text search query"),
    limit: int = Query(50, ge=1, le=200, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    current_user: User = Depends(current_active_user),
):
    """Full-text search across conversation titles, summaries, and transcripts."""
    return await conversation_controller.search_conversations(
        q, current_user, limit, offset
    )


@router.get("/{conversation_id}")
async def get_conversation_detail(
    conversation_id: str, current_user: User = Depends(current_active_user)
):
    """Get a specific conversation with full transcript details."""
    return await conversation_controller.get_conversation(conversation_id, current_user)


@router.get("/{conversation_id}/memories")
async def get_conversation_memories(
    conversation_id: str,
    limit: int = Query(100, ge=1, le=500, description="Max memories to return"),
    current_user: User = Depends(current_active_user),
):
    """Get memories extracted from a specific conversation."""
    return await conversation_controller.get_conversation_memories(
        conversation_id, current_user, limit
    )


# New reprocessing endpoints
@router.post("/{conversation_id}/reprocess-orphan")
async def reprocess_orphan(
    conversation_id: str, current_user: User = Depends(current_active_user)
):
    """Reprocess an orphan audio session (always_persist conversation with failed/pending transcription)."""
    return await conversation_controller.reprocess_orphan(conversation_id, current_user)


@router.post("/{conversation_id}/reprocess-transcript")
async def reprocess_transcript(
    conversation_id: str, current_user: User = Depends(current_active_user)
):
    """Reprocess transcript for a conversation. Users can only reprocess their own conversations."""
    return await conversation_controller.reprocess_transcript(
        conversation_id, current_user
    )


@router.post("/{conversation_id}/reprocess-memory")
async def reprocess_memory(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
    transcript_version_id: str = Query(default="active"),
):
    """Reprocess memory extraction for a specific transcript version. Users can only reprocess their own conversations."""
    return await conversation_controller.reprocess_memory(
        conversation_id, transcript_version_id, current_user
    )


@router.post("/{conversation_id}/reprocess-speakers")
async def reprocess_speakers(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
    transcript_version_id: str = Query(default="active"),
):
    """
    Re-run speaker identification/diarization on existing transcript.

    Creates a NEW transcript version with same text/words but re-identified speakers.
    Automatically chains memory reprocessing since speaker changes affect memory context.

    Args:
        conversation_id: Conversation to reprocess
        transcript_version_id: Which transcript version to use as source (default: "active")

    Returns:
        Job status with job_id and new version_id
    """
    return await conversation_controller.reprocess_speakers(
        conversation_id, transcript_version_id, current_user
    )


@router.post("/{conversation_id}/activate-transcript/{version_id}")
async def activate_transcript_version(
    conversation_id: str,
    version_id: str,
    current_user: User = Depends(current_active_user),
):
    """Activate a specific transcript version. Users can only modify their own conversations."""
    return await conversation_controller.activate_transcript_version(
        conversation_id, version_id, current_user
    )


@router.post("/{conversation_id}/activate-memory/{version_id}")
async def activate_memory_version(
    conversation_id: str,
    version_id: str,
    current_user: User = Depends(current_active_user),
):
    """Activate a specific memory version. Users can only modify their own conversations."""
    return await conversation_controller.activate_memory_version(
        conversation_id, version_id, current_user
    )


@router.get("/{conversation_id}/versions")
async def get_conversation_version_history(
    conversation_id: str, current_user: User = Depends(current_active_user)
):
    """Get version history for a conversation. Users can only access their own conversations."""
    return await conversation_controller.get_conversation_version_history(
        conversation_id, current_user
    )


@router.get("/{conversation_id}/waveform")
async def get_conversation_waveform(
    conversation_id: str, current_user: User = Depends(current_active_user)
):
    """
    Get or generate waveform visualization data for a conversation.

    This endpoint implements lazy generation:
    1. Check if waveform already exists in database
    2. If exists, return cached version immediately
    3. If not, generate synchronously and cache in database
    4. Return waveform data

    The waveform contains amplitude samples normalized to [-1.0, 1.0] range
    for visualization in the UI without needing to decode audio chunks.

    Returns:
        - samples: List[float] - Amplitude samples normalized to [-1, 1]
        - sample_rate: int - Samples per second (10)
        - duration_seconds: float - Total audio duration
    """
    from fastapi import HTTPException

    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.models.waveform import WaveformData
    from advanced_omi_backend.workers.waveform_jobs import generate_waveform_data

    # Verify conversation exists and user has access
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Check for existing waveform in database
    waveform = await WaveformData.find_one(
        WaveformData.conversation_id == conversation_id
    )

    # If waveform exists, return cached version
    if waveform:
        logger.info(
            f"Returning cached waveform for conversation {conversation_id[:12]}"
        )
        return waveform.model_dump(exclude={"id", "revision_id"})

    # Generate waveform on-demand
    logger.info(
        f"Generating waveform on-demand for conversation {conversation_id[:12]}"
    )

    waveform_dict = await generate_waveform_data(
        conversation_id=conversation_id, sample_rate=3
    )

    if not waveform_dict.get("success"):
        error_msg = waveform_dict.get("error", "Unknown error")
        logger.error(f"Waveform generation failed: {error_msg}")
        raise HTTPException(
            status_code=500, detail=f"Waveform generation failed: {error_msg}"
        )

    # Return generated waveform (already saved to database by generator)
    return {
        "samples": waveform_dict["samples"],
        "sample_rate": waveform_dict["sample_rate"],
        "duration_seconds": waveform_dict["duration_seconds"],
    }


@router.get("/{conversation_id}/metadata")
async def get_conversation_metadata(
    conversation_id: str, current_user: User = Depends(current_active_user)
) -> dict:
    """
    Get conversation metadata (duration, etc.) without loading audio.

    This endpoint provides lightweight access to conversation metadata,
    useful for the speaker service to check duration before deciding
    whether to chunk audio processing.

    Returns:
        {
            "conversation_id": str,
            "duration": float,  # Total duration in seconds
            "created_at": datetime,
            "has_audio": bool
        }
    """
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "conversation_id": conversation_id,
        "duration": conversation.audio_total_duration or 0.0,
        "created_at": conversation.created_at,
        "has_audio": (conversation.audio_total_duration or 0.0) > 0,
    }


@router.get("/{conversation_id}/audio-segments")
async def get_audio_segment(
    conversation_id: str,
    start: float = Query(0.0, description="Start time in seconds"),
    duration: Optional[float] = Query(
        None, description="Duration in seconds (omit for full audio)"
    ),
    current_user: User = Depends(current_active_user),
) -> Response:
    """
    Get audio segment from a conversation.

    This endpoint enables the speaker service to fetch audio in time-bounded
    segments without loading the entire file into memory. The speaker service
    controls chunk size based on its own memory constraints.

    Args:
        conversation_id: Conversation identifier
        start: Start time in seconds (default: 0.0)
        duration: Duration in seconds (if None, returns all audio from start)

    Returns:
        WAV audio bytes (16kHz, mono) for the requested time range
    """
    import time

    request_start = time.time()

    # Verify conversation exists and user has access
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Calculate end time
    total_duration = conversation.audio_total_duration or 0.0
    if total_duration == 0:
        raise HTTPException(
            status_code=404, detail="No audio available for this conversation"
        )

    if duration is None:
        end = total_duration
    else:
        end = min(start + duration, total_duration)

    # Validate time range
    if start < 0 or start >= total_duration:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid start time: {start}s (max: {total_duration}s)",
        )

    # Get audio chunks for time range
    try:
        wav_bytes = await reconstruct_audio_segment(
            conversation_id=conversation_id, start_time=start, end_time=end
        )
    except Exception as e:
        logger.error(
            f"Failed to reconstruct audio segment for {conversation_id[:12]}: {e}"
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to reconstruct audio: {str(e)}"
        )

    request_time = time.time() - request_start
    logger.info(
        f"Audio segment endpoint completed for {conversation_id[:12]}: "
        f"{start:.1f}s - {end:.1f}s ({end - start:.1f}s duration, "
        f"{len(wav_bytes) / 1024 / 1024:.2f} MB, "
        f"total request time: {request_time:.2f}s)"
    )

    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "Content-Disposition": f"attachment; filename=segment_{start}_{end}.wav",
            "X-Audio-Start": str(start),
            "X-Audio-End": str(end),
            "X-Audio-Duration": str(end - start),
        },
    )


@router.post("/{conversation_id}/star")
async def toggle_star(
    conversation_id: str, current_user: User = Depends(current_active_user)
):
    """Toggle the starred/favorite status of a conversation."""
    return await conversation_controller.toggle_star(conversation_id, current_user)


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    permanent: bool = Query(False, description="Permanently delete (admin only)"),
    current_user: User = Depends(current_active_user),
):
    """Soft delete a conversation (or permanently delete if admin)."""
    return await conversation_controller.delete_conversation(
        conversation_id, current_user, permanent
    )


@router.post("/{conversation_id}/restore")
async def restore_conversation(
    conversation_id: str, current_user: User = Depends(current_active_user)
):
    """Restore a soft-deleted conversation."""
    return await conversation_controller.restore_conversation(
        conversation_id, current_user
    )
