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
    AnnotationUpdate,
    DiarizationAnnotationCreate,
    EntityAnnotationCreate,
    InsertAnnotationCreate,
    MemoryAnnotationCreate,
    TitleAnnotationCreate,
    TranscriptAnnotationCreate,
)
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.services.knowledge_graph import get_knowledge_graph_service
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
                logger.info(f"Updated memory {annotation_data.memory_id} with corrected text")
            except Exception as e:
                logger.error(f"Error updating memory: {e}")
                # Annotation is saved, but memory update failed - log but don't fail the request
                logger.warning(f"Memory annotation {annotation.id} saved but memory update failed")

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
        if not active_transcript or annotation_data.segment_index >= len(
            active_transcript.segments
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
        if status == AnnotationStatus.ACCEPTED and old_status == AnnotationStatus.PENDING:
            if annotation.is_memory_annotation():
                # Update memory
                try:
                    memory_service = get_memory_service()
                    await memory_service.update_memory(
                        memory_id=annotation.memory_id,
                        content=annotation.corrected_text,
                        user_id=current_user.user_id,
                    )
                    logger.info(f"Applied suggestion to memory {annotation.memory_id}")
                except Exception as e:
                    logger.error(f"Error applying memory suggestion: {e}")
                    # Don't fail the status update if memory update fails
            elif annotation.is_transcript_annotation():
                # Update transcript segment
                try:
                    conversation = await Conversation.find_one(
                        Conversation.conversation_id == annotation.conversation_id,
                        Conversation.user_id == annotation.user_id,
                    )
                    if conversation:
                        transcript = conversation.active_transcript
                        if transcript and annotation.segment_index < len(transcript.segments):
                            transcript.segments[annotation.segment_index].text = (
                                annotation.corrected_text
                            )
                            await conversation.save()
                            logger.info(
                                f"Applied suggestion to transcript segment {annotation.segment_index}"
                            )
                except Exception as e:
                    logger.error(f"Error applying transcript suggestion: {e}")
                    # Don't fail the status update if segment update fails
            elif annotation.is_entity_annotation():
                # Update entity in Neo4j
                try:
                    kg_service = get_knowledge_graph_service()
                    update_kwargs = {}
                    if annotation.entity_field == "name":
                        update_kwargs["name"] = annotation.corrected_text
                    elif annotation.entity_field == "details":
                        update_kwargs["details"] = annotation.corrected_text
                    if update_kwargs:
                        await kg_service.update_entity(
                            entity_id=annotation.entity_id,
                            user_id=annotation.user_id,
                            **update_kwargs,
                        )
                        logger.info(f"Applied entity suggestion to entity {annotation.entity_id}")
                except Exception as e:
                    logger.error(f"Error applying entity suggestion: {e}")
                    # Don't fail the status update if entity update fails
            elif annotation.is_title_annotation():
                # Update conversation title
                try:
                    conversation = await Conversation.find_one(
                        Conversation.conversation_id == annotation.conversation_id,
                        Conversation.user_id == annotation.user_id,
                    )
                    if conversation:
                        conversation.title = annotation.corrected_text
                        await conversation.save()
                        logger.info(
                            f"Applied title suggestion to conversation {annotation.conversation_id}"
                        )
                except Exception as e:
                    logger.error(f"Error applying title suggestion: {e}")
                    # Don't fail the status update if title update fails

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


# === Generic Annotation Management ===


@router.delete("/{annotation_id}")
async def delete_annotation(
    annotation_id: str,
    current_user: User = Depends(current_active_user),
):
    """
    Delete an unprocessed annotation.

    - Only allows deleting annotations that haven't been applied yet (processed=False)
    - Returns 404 if not found, 400 if already processed
    """
    try:
        annotation = await Annotation.find_one(
            Annotation.id == annotation_id,
            Annotation.user_id == current_user.user_id,
        )
        if not annotation:
            raise HTTPException(status_code=404, detail="Annotation not found")

        if annotation.processed:
            raise HTTPException(status_code=400, detail="Cannot delete a processed annotation")

        await annotation.delete()
        logger.info(f"Deleted annotation {annotation_id}")

        return {"status": "deleted", "annotation_id": annotation_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting annotation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete annotation: {str(e)}",
        )


@router.patch("/{annotation_id}", response_model=AnnotationResponse)
async def update_annotation(
    annotation_id: str,
    update_data: AnnotationUpdate,
    current_user: User = Depends(current_active_user),
):
    """
    Update an unprocessed annotation in-place.

    - Only allows updating annotations that haven't been applied yet (processed=False)
    - Updates corrected_text, corrected_speaker, insert_text, or insert_segment_type
    - Replaces creating duplicate annotations when re-editing
    """
    try:
        annotation = await Annotation.find_one(
            Annotation.id == annotation_id,
            Annotation.user_id == current_user.user_id,
        )
        if not annotation:
            raise HTTPException(status_code=404, detail="Annotation not found")

        if annotation.processed:
            raise HTTPException(status_code=400, detail="Cannot update a processed annotation")

        if update_data.corrected_text is not None:
            annotation.corrected_text = update_data.corrected_text
        if update_data.corrected_speaker is not None:
            annotation.corrected_speaker = update_data.corrected_speaker
        if update_data.insert_text is not None:
            annotation.insert_text = update_data.insert_text
        if update_data.insert_segment_type is not None:
            annotation.insert_segment_type = update_data.insert_segment_type
        if update_data.insert_speaker is not None:
            annotation.insert_speaker = update_data.insert_speaker

        annotation.updated_at = datetime.now(timezone.utc)
        await annotation.save()
        logger.info(f"Updated annotation {annotation_id}")

        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating annotation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update annotation: {str(e)}",
        )


# === Insert Annotation Routes ===


@router.post("/insert", response_model=AnnotationResponse)
async def create_insert_annotation(
    annotation_data: InsertAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create an INSERT annotation to add a new segment between existing segments.

    - Validates conversation ownership and index bounds
    - Creates a pending annotation that will be applied with other annotations
    - insert_after_index=-1 means insert before the first segment
    """
    try:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        active_transcript = conversation.active_transcript
        if not active_transcript:
            raise HTTPException(status_code=400, detail="No active transcript found")

        segment_count = len(active_transcript.segments)
        if annotation_data.insert_after_index < -1 or annotation_data.insert_after_index >= segment_count:
            raise HTTPException(
                status_code=400,
                detail=f"insert_after_index must be between -1 and {segment_count - 1}",
            )

        if annotation_data.insert_segment_type not in ("event", "note", "speech"):
            raise HTTPException(
                status_code=400,
                detail="insert_segment_type must be 'event', 'note', or 'speech'",
            )

        annotation = Annotation(
            annotation_type=AnnotationType.INSERT,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            insert_after_index=annotation_data.insert_after_index,
            insert_text=annotation_data.insert_text,
            insert_segment_type=annotation_data.insert_segment_type,
            insert_speaker=annotation_data.insert_speaker,
            status=AnnotationStatus.PENDING,
            processed=False,
        )
        await annotation.save()
        logger.info(
            f"Created insert annotation {annotation.id} for conversation "
            f"{annotation_data.conversation_id} after index {annotation_data.insert_after_index}"
        )

        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating insert annotation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create insert annotation: {str(e)}",
        )


@router.get("/insert/{conversation_id}", response_model=List[AnnotationResponse])
async def get_insert_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all insert annotations for a conversation."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.INSERT,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return [AnnotationResponse.model_validate(a) for a in annotations]

    except Exception as e:
        logger.error(f"Error fetching insert annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch insert annotations: {str(e)}",
        )


# === Entity Annotation Routes ===


@router.post("/entity", response_model=AnnotationResponse)
async def create_entity_annotation(
    annotation_data: EntityAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for entity edit (name or details correction).

    - Validates user owns the entity
    - Creates annotation record for jargon/finetuning pipeline
    - Applies correction to Neo4j immediately
    - Marked as processed=False for downstream cron consumption

    Dual purpose: entity name corrections feed both the jargon pipeline
    (domain vocabulary for ASR) and the entity extraction pipeline
    (improving future extraction accuracy).
    """
    try:
        # Validate entity_field
        if annotation_data.entity_field not in ("name", "details"):
            raise HTTPException(
                status_code=400,
                detail="entity_field must be 'name' or 'details'",
            )

        # Verify entity exists and belongs to user
        kg_service = get_knowledge_graph_service()
        entity = await kg_service.get_entity(
            entity_id=annotation_data.entity_id,
            user_id=current_user.user_id,
        )
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        # Create annotation
        annotation = Annotation(
            annotation_type=AnnotationType.ENTITY,
            user_id=current_user.user_id,
            entity_id=annotation_data.entity_id,
            entity_field=annotation_data.entity_field,
            original_text=annotation_data.original_text,
            corrected_text=annotation_data.corrected_text,
            status=AnnotationStatus.ACCEPTED,
            processed=False,  # Unprocessed — jargon/finetuning cron will consume later
        )
        await annotation.save()
        logger.info(
            f"Created entity annotation {annotation.id} for entity {annotation_data.entity_id} "
            f"field={annotation_data.entity_field}"
        )

        # Apply correction to Neo4j immediately
        try:
            update_kwargs = {}
            if annotation_data.entity_field == "name":
                update_kwargs["name"] = annotation_data.corrected_text
            elif annotation_data.entity_field == "details":
                update_kwargs["details"] = annotation_data.corrected_text

            await kg_service.update_entity(
                entity_id=annotation_data.entity_id,
                user_id=current_user.user_id,
                **update_kwargs,
            )
            logger.info(f"Applied entity correction to Neo4j for entity {annotation_data.entity_id}")
        except Exception as e:
            logger.error(f"Error applying entity correction to Neo4j: {e}")
            # Annotation is saved but Neo4j update failed — log but don't fail the request

        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating entity annotation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create entity annotation: {str(e)}",
        )


@router.get("/entity/{entity_id}", response_model=List[AnnotationResponse])
async def get_entity_annotations(
    entity_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all annotations for an entity."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.ENTITY,
            Annotation.entity_id == entity_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return [AnnotationResponse.model_validate(a) for a in annotations]

    except Exception as e:
        logger.error(f"Error fetching entity annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch entity annotations: {str(e)}",
        )


# === Title Annotation Routes ===


@router.post("/title", response_model=AnnotationResponse)
async def create_title_annotation(
    annotation_data: TitleAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for conversation title edit.

    - Validates user owns conversation
    - Creates annotation record (instantly applied)
    - Updates conversation title immediately
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Create annotation (instantly applied)
        annotation = Annotation(
            annotation_type=AnnotationType.TITLE,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            original_text=annotation_data.original_text,
            corrected_text=annotation_data.corrected_text,
            status=AnnotationStatus.ACCEPTED,
            processed=True,
            processed_at=datetime.now(timezone.utc),
            processed_by="instant",
        )
        await annotation.save()
        logger.info(
            f"Created title annotation {annotation.id} for conversation {annotation_data.conversation_id}"
        )

        # Apply title change immediately
        try:
            conversation.title = annotation_data.corrected_text
            await conversation.save()
            logger.info(f"Updated title for conversation {annotation_data.conversation_id}")
        except Exception as e:
            logger.error(f"Error updating conversation title: {e}")
            # Annotation is saved but title update failed — log but don't fail the request

        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating title annotation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create title annotation: {str(e)}",
        )


@router.get("/title/{conversation_id}", response_model=List[AnnotationResponse])
async def get_title_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all title annotations for a conversation (audit trail)."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.TITLE,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return [AnnotationResponse.model_validate(a) for a in annotations]

    except Exception as e:
        logger.error(f"Error fetching title annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch title annotations: {str(e)}",
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
        if not active_transcript or annotation_data.segment_index >= len(
            active_transcript.segments
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

        # Copy segments and apply corrections (most recent annotation wins)
        corrected_segments = []
        for segment_idx, segment in enumerate(active_transcript.segments):
            # Find annotation for this segment index (most recent wins if duplicates)
            annotations_for_segment = sorted(
                [a for a in annotations if a.segment_index == segment_idx],
                key=lambda a: a.updated_at,
                reverse=True,
            )
            annotation_for_segment = annotations_for_segment[0] if annotations_for_segment else None

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
            conversation_id=conversation_id,
            priority=JobPriority.NORMAL,
        )

        return JSONResponse(
            content={
                "message": "Diarization annotations applied",
                "version_id": new_version_id,
                "applied_count": len(annotations),
                "status": "success",
            }
        )

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
            return JSONResponse(
                content={
                    "message": "No pending annotations to apply",
                    "diarization_count": 0,
                    "transcript_count": 0,
                }
            )

        # Separate by type
        diarization_annotations = [
            a for a in annotations if a.annotation_type == AnnotationType.DIARIZATION
        ]
        transcript_annotations = [
            a for a in annotations if a.annotation_type == AnnotationType.TRANSCRIPT
        ]
        insert_annotations = [
            a for a in annotations if a.annotation_type == AnnotationType.INSERT
        ]

        # Get active transcript
        active_transcript = conversation.active_transcript
        if not active_transcript:
            raise HTTPException(status_code=404, detail="No active transcript found")

        # Create new version with ALL corrections applied
        import uuid

        new_version_id = str(uuid.uuid4())
        corrected_segments = []

        # For diarization/transcript: if multiple annotations exist for same segment,
        # pick the most recently updated one
        for segment_idx, segment in enumerate(active_transcript.segments):
            corrected_segment = segment.model_copy()

            # Apply diarization correction (most recent wins)
            diar_for_segment = sorted(
                [a for a in diarization_annotations if a.segment_index == segment_idx],
                key=lambda a: a.updated_at,
                reverse=True,
            )
            if diar_for_segment:
                corrected_segment.speaker = diar_for_segment[0].corrected_speaker

            # Apply transcript correction (most recent wins)
            transcript_for_segment = sorted(
                [a for a in transcript_annotations if a.segment_index == segment_idx],
                key=lambda a: a.updated_at,
                reverse=True,
            )
            if transcript_for_segment:
                corrected_segment.text = transcript_for_segment[0].corrected_text

            corrected_segments.append(corrected_segment)

        # Apply inserts from highest index to lowest (stable indexing)
        if insert_annotations:
            sorted_inserts = sorted(
                insert_annotations,
                key=lambda a: a.insert_after_index,
                reverse=True,
            )
            for ins in sorted_inserts:
                idx = ins.insert_after_index  # -1 = before first
                insert_pos = idx + 1  # Convert to list insertion position

                # Calculate timing from surrounding segments
                if insert_pos > 0 and insert_pos <= len(corrected_segments):
                    boundary_time = corrected_segments[insert_pos - 1].end
                elif insert_pos == 0 and corrected_segments:
                    boundary_time = corrected_segments[0].start
                else:
                    boundary_time = 0.0

                new_segment = Conversation.SpeakerSegment(
                    start=boundary_time,
                    end=boundary_time,
                    text=ins.insert_text or "",
                    speaker=ins.insert_speaker or "",
                    segment_type=ins.insert_segment_type or "event",
                )
                corrected_segments.insert(insert_pos, new_segment)

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
                "insert_count": len(insert_annotations),
            },
            set_as_active=True,
        )

        await conversation.save()
        logger.info(
            f"Applied {len(annotations)} annotations "
            f"(diarization: {len(diarization_annotations)}, "
            f"transcript: {len(transcript_annotations)}, "
            f"insert: {len(insert_annotations)})"
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
            conversation_id=conversation_id,
            priority=JobPriority.NORMAL,
        )

        return JSONResponse(
            content={
                "message": (
                    f"Applied {len(diarization_annotations)} diarization, "
                    f"{len(transcript_annotations)} transcript, and "
                    f"{len(insert_annotations)} insert annotations"
                ),
                "version_id": new_version_id,
                "diarization_count": len(diarization_annotations),
                "transcript_count": len(transcript_annotations),
                "insert_count": len(insert_annotations),
                "status": "success",
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error applying annotations: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply annotations: {str(e)}",
        )
