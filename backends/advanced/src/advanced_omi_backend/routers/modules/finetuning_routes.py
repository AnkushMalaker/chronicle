"""
Fine-tuning routes for Chronicle API.

Handles sending annotation corrections to speaker recognition service for training.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.annotation import Annotation, AnnotationType
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/finetuning", tags=["finetuning"])


@router.post("/process-annotations")
async def process_annotations_for_training(
    current_user: User = Depends(current_active_user),
    annotation_type: Optional[str] = Query("diarization", description="Type of annotations to process"),
):
    """
    Send processed annotations to speaker recognition service for training.

    - Finds annotations that have been applied (processed=True, processed_by="apply")
    - Sends corrections to speaker service for model fine-tuning
    - Updates annotations with training metadata (processed_by includes "training")

    Args:
        annotation_type: Type of annotations to process (default: "diarization")

    Returns:
        Training job status with count of annotations processed
    """
    try:
        # Only admins can trigger training for now (can expand to per-user later)
        if not current_user.is_superuser:
            raise HTTPException(
                status_code=403,
                detail="Only administrators can trigger model training"
            )

        # Find annotations ready for training
        # Criteria: processed=True (applied to transcript), but not yet sent to training
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.processed == True,
        ).to_list()

        # Filter out already trained annotations (processed_by contains "training")
        ready_for_training = [
            a for a in annotations
            if a.processed_by and "training" not in a.processed_by
        ]

        if not ready_for_training:
            return JSONResponse(content={
                "message": "No annotations ready for training",
                "processed_count": 0
            })

        # Import required modules
        from advanced_omi_backend.models.conversation import Conversation
        from advanced_omi_backend.speaker_recognition_client import (
            SpeakerRecognitionClient,
        )
        from advanced_omi_backend.utils.audio_chunk_utils import (
            reconstruct_audio_segment,
        )

        # Initialize speaker client
        speaker_client = SpeakerRecognitionClient()
        
        if not speaker_client.enabled:
            return JSONResponse(content={
                "message": "Speaker recognition service is not enabled",
                "processed_count": 0,
                "status": "error"
            }, status_code=503)

        # Track processing statistics
        enrolled_count = 0
        appended_count = 0
        failed_count = 0
        errors = []

        for annotation in ready_for_training:
            try:
                # 1. Get conversation and segment timing
                conversation = await Conversation.find_one(
                    Conversation.conversation_id == annotation.conversation_id
                )
                
                if not conversation or not conversation.active_transcript:
                    logger.warning(f"Conversation {annotation.conversation_id} not found or has no transcript")
                    failed_count += 1
                    errors.append(f"Conversation {annotation.conversation_id[:8]} not found")
                    continue

                # Validate segment index
                if annotation.segment_index >= len(conversation.active_transcript.segments):
                    logger.warning(f"Invalid segment index {annotation.segment_index} for conversation {annotation.conversation_id}")
                    failed_count += 1
                    errors.append(f"Invalid segment index {annotation.segment_index}")
                    continue

                segment = conversation.active_transcript.segments[annotation.segment_index]

                # 2. Extract audio segment from MongoDB
                logger.info(
                    f"Extracting audio for conversation {annotation.conversation_id[:8]}... "
                    f"segment {annotation.segment_index} ({segment.start:.2f}s - {segment.end:.2f}s)"
                )
                
                wav_bytes = await reconstruct_audio_segment(
                    conversation_id=annotation.conversation_id,
                    start_time=segment.start,
                    end_time=segment.end
                )

                if not wav_bytes:
                    logger.warning(f"No audio data for annotation {annotation.id}")
                    failed_count += 1
                    errors.append(f"No audio for segment {annotation.segment_index}")
                    continue

                logger.info(f"Extracted {len(wav_bytes) / 1024:.1f} KB of audio")

                # 3. Check if speaker exists
                existing_speaker = await speaker_client.get_speaker_by_name(
                    speaker_name=annotation.corrected_speaker,
                    user_id=1  # TODO: Map Chronicle user_id to speaker service user_id
                )

                if existing_speaker:
                    # APPEND to existing speaker
                    logger.info(f"Appending to existing speaker: {annotation.corrected_speaker}")
                    result = await speaker_client.append_to_speaker(
                        speaker_id=existing_speaker["id"],
                        audio_data=wav_bytes
                    )
                    
                    if "error" in result:
                        logger.error(f"Failed to append to speaker: {result}")
                        failed_count += 1
                        errors.append(f"Append failed: {result.get('error')}")
                        continue
                    
                    appended_count += 1
                    logger.info(f"✅ Successfully appended to speaker '{annotation.corrected_speaker}'")
                else:
                    # ENROLL new speaker
                    logger.info(f"Enrolling new speaker: {annotation.corrected_speaker}")
                    result = await speaker_client.enroll_new_speaker(
                        speaker_name=annotation.corrected_speaker,
                        audio_data=wav_bytes,
                        user_id=1  # TODO: Map Chronicle user_id to speaker service user_id
                    )
                    
                    if "error" in result:
                        logger.error(f"Failed to enroll speaker: {result}")
                        failed_count += 1
                        errors.append(f"Enroll failed: {result.get('error')}")
                        continue
                    
                    enrolled_count += 1
                    logger.info(f"✅ Successfully enrolled new speaker '{annotation.corrected_speaker}'")

                # 4. Mark annotation as trained
                if annotation.processed_by:
                    annotation.processed_by = f"{annotation.processed_by},training"
                else:
                    annotation.processed_by = "training"
                annotation.updated_at = datetime.now(timezone.utc)
                await annotation.save()

            except Exception as e:
                logger.error(f"Error processing annotation {annotation.id}: {e}", exc_info=True)
                failed_count += 1
                errors.append(f"Exception: {str(e)[:50]}")
                continue

        total_processed = enrolled_count + appended_count
        logger.info(
            f"Training complete: {total_processed} processed "
            f"({enrolled_count} new, {appended_count} appended, {failed_count} failed)"
        )

        return JSONResponse(content={
            "message": "Training complete",
            "enrolled_new_speakers": enrolled_count,
            "appended_to_existing": appended_count,
            "total_processed": total_processed,
            "failed_count": failed_count,
            "errors": errors[:10] if errors else [],  # Limit error list
            "status": "success" if total_processed > 0 else "partial_failure"
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing annotations for training: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process annotations for training: {str(e)}",
        )


@router.get("/status")
async def get_finetuning_status(
    current_user: User = Depends(current_active_user),
):
    """
    Get fine-tuning status and statistics.

    Returns:
        - pending_annotation_count: Annotations not yet applied
        - applied_annotation_count: Annotations applied but not trained
        - trained_annotation_count: Annotations sent to training
        - last_training_run: Timestamp of last training job
        - cron_status: Cron job schedule and last run info
    """
    try:
        # Count annotations by status
        pending_count = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.processed == False,
        ).count()

        # Get all processed annotations
        all_processed = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.processed == True,
        ).to_list()

        # Split into trained vs not-yet-trained
        trained_annotations = [
            a for a in all_processed
            if a.processed_by and "training" in a.processed_by
        ]
        applied_not_trained = [
            a for a in all_processed
            if not a.processed_by or "training" not in a.processed_by
        ]

        applied_count = len(applied_not_trained)
        trained_count = len(trained_annotations)

        # Get last training run timestamp
        last_training_run = None
        if trained_annotations:
            # Find most recent trained annotation
            latest_trained = max(
                trained_annotations,
                key=lambda a: a.updated_at if a.updated_at else datetime.min.replace(tzinfo=timezone.utc)
            )
            last_training_run = latest_trained.updated_at.isoformat() if latest_trained.updated_at else None

        # TODO: Get cron job status from scheduler
        cron_status = {
            "enabled": False,  # Placeholder
            "schedule": "0 2 * * *",  # Example: daily at 2 AM
            "last_run": None,
            "next_run": None,
        }

        return JSONResponse(content={
            "pending_annotation_count": pending_count,
            "applied_annotation_count": applied_count,
            "trained_annotation_count": trained_count,
            "last_training_run": last_training_run,
            "cron_status": cron_status,
        })

    except Exception as e:
        logger.error(f"Error fetching fine-tuning status: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch fine-tuning status: {str(e)}",
        )
