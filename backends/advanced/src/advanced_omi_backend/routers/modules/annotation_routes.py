"""
Annotation routes for Chronicle API.

Handles annotation CRUD operations for memories and transcripts.
Supports both user edits and AI-powered suggestions.
"""

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.annotation import (
    Annotation,
    AnnotationResponse,
    AnnotationStatus,
    AnnotationType,
    DiarizationAnnotationCreate,
    MemoryAnnotationCreate,
    TranscriptAnnotationCreate,
)
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/annotations", tags=["annotations"])


@router.post("/memory", response_model=AnnotationResponse)
async def create_memory_annotation(
    annotation_data: MemoryAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for memory edit.

    - Validates user owns memory
    - Creates annotation record
    - Updates memory content in vector store
    - Re-embeds if content changed
    """
    try:
        memory_service = get_memory_service()

        # Verify memory ownership
        try:
            memory = await memory_service.get_memory(
                annotation_data.memory_id, current_user.user_id
            )
            if not memory:
                raise HTTPException(status_code=404, detail="Memory not found")
        except Exception as e:
            logger.error(f"Error fetching memory: {e}")
            raise HTTPException(status_code=404, detail="Memory not found")

        # Create annotation
        annotation = Annotation(
            annotation_type=AnnotationType.MEMORY,
            user_id=current_user.user_id,
            memory_id=annotation_data.memory_id,
            original_text=annotation_data.original_text,
            corrected_text=annotation_data.corrected_text,
            status=annotation_data.status,
        )
        await annotation.save()
        logger.info(
            f"Created memory annotation {annotation.id} for memory {annotation_data.memory_id}"
        )

        # Update memory content if accepted
        if annotation.status == AnnotationStatus.ACCEPTED:
            try:
                await memory_service.update_memory(
                    memory_id=annotation_data.memory_id,
                    content=annotation_data.corrected_text,
                    user_id=current_user.user_id,
                )
                logger.info(
                    f"Updated memory {annotation_data.memory_id} with corrected text"
                )
            except Exception as e:
                logger.error(f"Error updating memory: {e}")
                # Annotation is saved, but memory update failed - log but don't fail the request
                logger.warning(
                    f"Memory annotation {annotation.id} saved but memory update failed"
                )

        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating memory annotation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create memory annotation: {str(e)}",
        )


@router.post("/transcript", response_model=AnnotationResponse)
async def create_transcript_annotation(
    annotation_data: TranscriptAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for transcript segment edit.

    - Validates user owns conversation
    - Creates annotation record (NOT applied to transcript yet)
    - Annotation is marked as unprocessed (processed=False)
    - Visual indication in UI (pending badge)
    - Use unified apply endpoint to apply all annotations together
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Validate segment index
        active_transcript = conversation.active_transcript
        if (
            not active_transcript
            or annotation_data.segment_index >= len(active_transcript.segments)
        ):
            raise HTTPException(status_code=400, detail="Invalid segment index")

        segment = active_transcript.segments[annotation_data.segment_index]

        # Create annotation (NOT applied yet)
        annotation = Annotation(
            annotation_type=AnnotationType.TRANSCRIPT,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            segment_index=annotation_data.segment_index,
            original_text=segment.text,  # Use current segment text
            corrected_text=annotation_data.corrected_text,
            status=AnnotationStatus.PENDING,  # Changed from ACCEPTED
            processed=False,  # Not applied yet
        )
        await annotation.save()
        logger.info(
            f"Created transcript annotation {annotation.id} for conversation {annotation_data.conversation_id} segment {annotation_data.segment_index}"
        )

        # Do NOT modify transcript immediately
        # Do NOT trigger memory reprocessing yet
        # User must click "Apply Changes" button to apply all annotations together

        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating transcript annotation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create transcript annotation: {str(e)}",
        )


@router.get("/memory/{memory_id}", response_model=List[AnnotationResponse])
async def get_memory_annotations(
    memory_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all annotations for a memory."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.MEMORY,
            Annotation.memory_id == memory_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return [AnnotationResponse.model_validate(a) for a in annotations]

    except Exception as e:
        logger.error(f"Error fetching memory annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch memory annotations: {str(e)}",
        )


@router.get("/transcript/{conversation_id}", response_model=List[AnnotationResponse])
async def get_transcript_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all annotations for a conversation's transcript."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.TRANSCRIPT,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return [AnnotationResponse.model_validate(a) for a in annotations]

    except Exception as e:
        logger.error(f"Error fetching transcript annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch transcript annotations: {str(e)}",
        )


@router.patch("/{annotation_id}/status")
async def update_annotation_status(
    annotation_id: str,
    status: AnnotationStatus,
    current_user: User = Depends(current_active_user),
):
    """
    Accept or reject AI-generated suggestions.

    Used for pending model suggestions in the UI.
    """
    try:
        annotation = await Annotation.find_one(
            Annotation.id == annotation_id,
            Annotation.user_id == current_user.user_id,
        )
        if not annotation:
            raise HTTPException(status_code=404, detail="Annotation not found")

        old_status = annotation.status
        annotation.status = status
        annotation.updated_at = datetime.now(timezone.utc)

        # If accepting a pending suggestion, apply the correction
        if (
            status == AnnotationStatus.ACCEPTED
            and old_status == AnnotationStatus.PENDING
        ):
            if annotation.is_memory_annotation():
                # Update memory
                try:
                    memory_service = get_memory_service()
                    await memory_service.update_memory(
                        memory_id=annotation.memory_id,
                        content=annotation.corrected_text,
                        user_id=current_user.user_id,
                    )
                    logger.info(
                        f"Applied suggestion to memory {annotation.memory_id}"
                    )
                except Exception as e:
                    logger.error(f"Error applying memory suggestion: {e}")
                    # Don't fail the status update if memory update fails
            elif annotation.is_transcript_annotation():
                # Update transcript segment
                try:
                    conversation = await Conversation.find_one(
                        Conversation.conversation_id == annotation.conversation_id,
                        Conversation.user_id == annotation.user_id
                    )
                    if conversation:
                        transcript = conversation.active_transcript
                        if (
                            transcript
                            and annotation.segment_index < len(transcript.segments)
                        ):
                            transcript.segments[
                                annotation.segment_index
                            ].text = annotation.corrected_text
                            await conversation.save()
                            logger.info(
                                f"Applied suggestion to transcript segment {annotation.segment_index}"
                            )
                except Exception as e:
                    logger.error(f"Error applying transcript suggestion: {e}")
                    # Don't fail the status update if segment update fails

        await annotation.save()
        logger.info(f"Updated annotation {annotation_id} status to {status}")

        return {"status": "updated", "annotation_id": annotation_id, "new_status": status}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating annotation status: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update annotation status: {str(e)}",
        )


# === Diarization Annotation Routes ===


@router.post("/diarization", response_model=AnnotationResponse)
async def create_diarization_annotation(
    annotation_data: DiarizationAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for speaker identification correction.

    - Validates user owns conversation
    - Creates annotation record (NOT applied to transcript yet)
    - Annotation is marked as unprocessed (processed=False)
    - Visual indication in UI (strikethrough + corrected name)
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Validate segment index
        active_transcript = conversation.active_transcript
        if (
            not active_transcript
            or annotation_data.segment_index >= len(active_transcript.segments)
        ):
            raise HTTPException(status_code=400, detail="Invalid segment index")

        # Create annotation (NOT applied yet)
        annotation = Annotation(
            annotation_type=AnnotationType.DIARIZATION,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            segment_index=annotation_data.segment_index,
            original_speaker=annotation_data.original_speaker,
            corrected_speaker=annotation_data.corrected_speaker,
            segment_start_time=annotation_data.segment_start_time,
            original_text="",  # Not used for diarization
            corrected_text="",  # Not used for diarization
            status=annotation_data.status,
            processed=False,  # Not applied or sent to training yet
        )
        await annotation.save()
        logger.info(
            f"Created diarization annotation {annotation.id} for conversation {annotation_data.conversation_id} segment {annotation_data.segment_index}"
        )

        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating diarization annotation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create diarization annotation: {str(e)}",
        )


@router.get("/diarization/{conversation_id}", response_model=List[AnnotationResponse])
async def get_diarization_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all diarization annotations for a conversation."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return [AnnotationResponse.model_validate(a) for a in annotations]

    except Exception as e:
        logger.error(f"Error fetching diarization annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch diarization annotations: {str(e)}",
        )


@router.post("/diarization/{conversation_id}/apply")
async def apply_diarization_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """
    Apply pending diarization annotations to create new transcript version.

    - Finds all unprocessed diarization annotations for conversation
    - Creates NEW transcript version with corrected speaker labels
    - Marks annotations as processed (processed=True, processed_by="apply")
    - Chains memory reprocessing since speaker changes affect meaning
    - Returns job status with new version_id
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Get unprocessed diarization annotations
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
            Annotation.processed == False,  # Only unprocessed
        ).to_list()

        if not annotations:
            return JSONResponse(
                content={"message": "No pending annotations to apply", "applied_count": 0}
            )

        # Get active transcript version
        active_transcript = conversation.active_transcript
        if not active_transcript:
            raise HTTPException(status_code=404, detail="No active transcript found")

        # Create NEW transcript version with corrected speakers
        import uuid
        new_version_id = str(uuid.uuid4())

        # Copy segments and apply corrections
        corrected_segments = []
        for segment_idx, segment in enumerate(active_transcript.segments):
            # Find annotation for this segment index
            annotation_for_segment = next(
                (a for a in annotations if a.segment_index == segment_idx), None
            )

            if annotation_for_segment:
                # Apply correction
                corrected_segment = segment.model_copy()
                corrected_segment.speaker = annotation_for_segment.corrected_speaker
                corrected_segments.append(corrected_segment)
            else:
                # No correction, keep original
                corrected_segments.append(segment.model_copy())

        # Add new version
        conversation.add_transcript_version(
            version_id=new_version_id,
            transcript=active_transcript.transcript,  # Same transcript text
            words=active_transcript.words,  # Same word timings
            segments=corrected_segments,  # Corrected speaker labels
            provider=active_transcript.provider,
            model=active_transcript.model,
            processing_time_seconds=None,
            metadata={
                "reprocessing_type": "diarization_annotations",
                "source_version_id": active_transcript.version_id,
                "trigger": "manual_annotation_apply",
                "applied_annotation_count": len(annotations),
            },
            set_as_active=True,
        )

        await conversation.save()
        logger.info(
            f"Created new transcript version {new_version_id} with {len(annotations)} diarization corrections"
        )

        # Mark annotations as processed
        for annotation in annotations:
            annotation.processed = True
            annotation.processed_at = datetime.now(timezone.utc)
            annotation.processed_by = "apply"
            await annotation.save()

        # Chain memory reprocessing
        from advanced_omi_backend.models.job import JobPriority
        from advanced_omi_backend.workers.memory_jobs import enqueue_memory_processing

        enqueue_memory_processing(
            client_id=conversation.client_id,
            user_id=current_user.user_id,
            user_email=current_user.email,
            conversation_id=conversation_id,
            priority=JobPriority.NORMAL,
        )

        return JSONResponse(content={
            "message": "Diarization annotations applied",
            "version_id": new_version_id,
            "applied_count": len(annotations),
            "status": "success"
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error applying diarization annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply diarization annotations: {str(e)}",
        )


@router.post("/{conversation_id}/apply")
async def apply_all_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """
    Apply all pending annotations (diarization + transcript) to create new version.

    - Finds all unprocessed annotations (both DIARIZATION and TRANSCRIPT types)
    - Creates ONE new transcript version with all changes applied
    - Marks all annotations as processed
    - Triggers memory reprocessing once
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Get ALL unprocessed annotations (both types)
        annotations = await Annotation.find(
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
            Annotation.processed == False,
        ).to_list()

        if not annotations:
            return JSONResponse(content={
                "message": "No pending annotations to apply",
                "diarization_count": 0,
                "transcript_count": 0,
            })

        # Separate by type
        diarization_annotations = [a for a in annotations if a.annotation_type == AnnotationType.DIARIZATION]
        transcript_annotations = [a for a in annotations if a.annotation_type == AnnotationType.TRANSCRIPT]

        # Get active transcript
        active_transcript = conversation.active_transcript
        if not active_transcript:
            raise HTTPException(status_code=404, detail="No active transcript found")

        # Create new version with ALL corrections applied
        import uuid
        new_version_id = str(uuid.uuid4())
        corrected_segments = []

        for segment_idx, segment in enumerate(active_transcript.segments):
            corrected_segment = segment.model_copy()

            # Apply diarization correction (if exists)
            diar_annotation = next(
                (a for a in diarization_annotations if a.segment_index == segment_idx),
                None
            )
            if diar_annotation:
                corrected_segment.speaker = diar_annotation.corrected_speaker

            # Apply transcript correction (if exists)
            transcript_annotation = next(
                (a for a in transcript_annotations if a.segment_index == segment_idx),
                None
            )
            if transcript_annotation:
                corrected_segment.text = transcript_annotation.corrected_text

            corrected_segments.append(corrected_segment)

        # Add new version
        conversation.add_transcript_version(
            version_id=new_version_id,
            transcript=active_transcript.transcript,
            words=active_transcript.words,  # Preserved (may be misaligned for text edits)
            segments=corrected_segments,
            provider=active_transcript.provider,
            model=active_transcript.model,
            metadata={
                "reprocessing_type": "unified_annotations",
                "source_version_id": active_transcript.version_id,
                "trigger": "manual_annotation_apply",
                "diarization_count": len(diarization_annotations),
                "transcript_count": len(transcript_annotations),
            },
            set_as_active=True,
        )

        await conversation.save()
        logger.info(
            f"Applied {len(annotations)} annotations (diarization: {len(diarization_annotations)}, transcript: {len(transcript_annotations)})"
        )

        # Mark all annotations as processed
        for annotation in annotations:
            annotation.processed = True
            annotation.processed_at = datetime.now(timezone.utc)
            annotation.processed_by = "apply"
            annotation.status = AnnotationStatus.ACCEPTED
            await annotation.save()

        # Trigger memory reprocessing (once for all changes)
        from advanced_omi_backend.models.job import JobPriority
        from advanced_omi_backend.workers.memory_jobs import enqueue_memory_processing

        enqueue_memory_processing(
            client_id=conversation.client_id,
            user_id=current_user.user_id,
            user_email=current_user.email,
            conversation_id=conversation_id,
            priority=JobPriority.NORMAL,
        )

        return JSONResponse(content={
            "message": f"Applied {len(diarization_annotations)} diarization and {len(transcript_annotations)} transcript annotations",
            "version_id": new_version_id,
            "diarization_count": len(diarization_annotations),
            "transcript_count": len(transcript_annotations),
            "status": "success",
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error applying annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply annotations: {str(e)}",
        )
