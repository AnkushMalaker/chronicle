"""
Fine-tuning routes for Chronicle API.

Handles sending annotation corrections to speaker recognition service for training
and cron job management for automated tasks.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.constants import is_non_enrollable_speaker
from advanced_omi_backend.cron_scheduler import get_scheduler
from advanced_omi_backend.models.annotation import Annotation, AnnotationType
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.services.observability.system_events import record_event
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
from advanced_omi_backend.users import User
from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_audio_segment
from advanced_omi_backend.workers.finetuning_jobs import run_asr_finetuning_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/finetuning", tags=["finetuning"])

# Segments whose start is within this many seconds of the annotation's recorded
# start are treated as the same segment when keying by time.
_SEGMENT_START_TOLERANCE = 0.25


def _resolve_annotated_segment(segments, segment_index, segment_start_time):
    """Find the segment an annotation refers to.

    Prefers matching by ``segment_start_time`` (stable across re-ordering) and
    falls back to the stored index. Returns the segment or ``None`` if neither
    locates a valid segment.
    """
    if segment_start_time is not None:
        best = None
        best_delta = _SEGMENT_START_TOLERANCE
        for seg in segments:
            delta = abs(seg.start - segment_start_time)
            if delta <= best_delta:
                best = seg
                best_delta = delta
        if best is not None:
            return best
    if segment_index is not None and 0 <= segment_index < len(segments):
        return segments[segment_index]
    return None


async def _record_training_failure(annotation: Annotation, reason: str) -> None:
    """Persist a training failure on the annotation so it can be surfaced/cleared.

    Without this the annotation stays in the "applied, not trained" bucket and
    re-fails on every run with no visible record — the Fine-tuning page appears
    stuck. Recording the attempt + reason lets the UI show the failure and offer
    retry/discard.
    """
    annotation.training_attempts = (annotation.training_attempts or 0) + 1
    annotation.training_error = reason
    annotation.updated_at = datetime.now(timezone.utc)
    await annotation.save()


@router.post("/process-annotations")
async def process_annotations_for_training(
    current_user: User = Depends(current_active_user),
    annotation_type: Optional[str] = Query(
        "diarization", description="Type of annotations to process"
    ),
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
                status_code=403, detail="Only administrators can trigger model training"
            )

        # Find annotations ready for training
        # Criteria: processed=True (applied to transcript), but not yet sent to training
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.processed == True,
        ).to_list()

        # Filter out already trained annotations (processed_by contains "training")
        ready_for_training = [
            a
            for a in annotations
            if not a.processed_by or "training" not in a.processed_by
        ]

        if not ready_for_training:
            return JSONResponse(
                content={
                    "message": "No annotations ready for training",
                    "processed_count": 0,
                }
            )

        # Initialize speaker client
        speaker_client = SpeakerRecognitionClient()

        if not speaker_client.enabled:
            return JSONResponse(
                content={
                    "message": "Speaker recognition service is not enabled",
                    "processed_count": 0,
                    "status": "error",
                },
                status_code=503,
            )

        # Track processing statistics
        enrolled_count = 0
        appended_count = 0
        failed_count = 0
        errors = []

        for annotation in ready_for_training:
            try:
                # Noise and placeholder "Unknown Speaker N" labels are not real
                # people — never enroll them as voiceprints. Mark trained so they
                # aren't retried, then skip.
                if is_non_enrollable_speaker(annotation.corrected_speaker):
                    annotation.processed_by = (
                        f"{annotation.processed_by},training"
                        if annotation.processed_by
                        else "training"
                    )
                    annotation.updated_at = datetime.now(timezone.utc)
                    await annotation.save()
                    continue

                # 1. Get conversation and segment timing
                conversation = await Conversation.find_one(
                    Conversation.conversation_id == annotation.conversation_id
                )

                if not conversation or not conversation.active_transcript:
                    failed_count += 1
                    reason = f"Conversation {annotation.conversation_id[:8]} not found"
                    errors.append(reason)
                    await _record_training_failure(annotation, reason)
                    continue

                # Resolve the segment by start-time when recorded (robust to the
                # segment list being re-ordered/filtered by a reprocess between
                # annotation and apply); fall back to the stored index.
                segment = _resolve_annotated_segment(
                    conversation.active_transcript.segments,
                    annotation.segment_index,
                    annotation.segment_start_time,
                )
                if segment is None:
                    failed_count += 1
                    reason = f"Invalid segment index {annotation.segment_index}"
                    errors.append(reason)
                    await _record_training_failure(annotation, reason)
                    continue

                # Guard corrupt segment times (e.g. start > end, or start beyond
                # the audio) — reconstruct_audio_segment would raise. Skip the
                # voiceprint enrollment for this segment; the relabel already
                # applied (apply doesn't touch audio). Surface it on the System
                # Errors page as a data-integrity issue (the transcript itself is
                # corrupt — usually fixable by reprocessing the conversation).
                # NOTE: reconstruct_audio_segment clamps end_time to the audio
                # duration but NOT start_time, so a segment starting beyond the audio
                # (streaming-reconnect timeline inflation) yields end < start and
                # raises — catch that here too, not just start > end.
                audio_dur = conversation.audio_total_duration or 0.0
                if not (segment.end > segment.start >= 0) or (
                    audio_dur and segment.start >= audio_dur
                ):
                    failed_count += 1
                    reason = (
                        f"Segment {annotation.segment_index} has invalid times "
                        f"({segment.start:.1f}s-{segment.end:.1f}s); skipped enrollment"
                    )
                    errors.append(reason)
                    await _record_training_failure(annotation, reason)
                    logger.warning(
                        f"Skipping enrollment for {annotation.conversation_id[:8]} "
                        f"segment {annotation.segment_index}: invalid times "
                        f"{segment.start:.1f}-{segment.end:.1f}"
                    )
                    await record_event(
                        severity="warning",
                        category="data_integrity",
                        source="speaker_triage_enroll",
                        title="Transcript segment has invalid times",
                        detail=(
                            f"Segment {annotation.segment_index} of conversation "
                            f"{annotation.conversation_id} has start={segment.start:.2f}s "
                            f"end={segment.end:.2f}s (start must be < end and within the "
                            f"audio). Voiceprint enrollment skipped; reprocess the "
                            f"conversation's transcript to regenerate segment times."
                        ),
                        conversation_id=annotation.conversation_id,
                        metadata={
                            "segment_index": annotation.segment_index,
                            "start": segment.start,
                            "end": segment.end,
                        },
                    )
                    continue

                # 2. Extract audio segment from MongoDB
                logger.info(
                    f"Extracting audio for conversation {annotation.conversation_id[:8]}... "
                    f"segment {annotation.segment_index} ({segment.start:.2f}s - {segment.end:.2f}s)"
                )

                wav_bytes = await reconstruct_audio_segment(
                    conversation_id=annotation.conversation_id,
                    start_time=segment.start,
                    end_time=segment.end,
                )

                if not wav_bytes:
                    logger.warning(f"No audio data for annotation {annotation.id}")
                    failed_count += 1
                    reason = f"No audio for segment {annotation.segment_index}"
                    errors.append(reason)
                    await _record_training_failure(annotation, reason)
                    continue

                logger.info(f"Extracted {len(wav_bytes) / 1024:.1f} KB of audio")

                # 3. Check if speaker exists
                existing_speaker = await speaker_client.get_speaker_by_name(
                    speaker_name=annotation.corrected_speaker,
                    user_id=1,  # TODO: Map Chronicle user_id to speaker service user_id
                )

                if existing_speaker:
                    # APPEND to existing speaker
                    logger.info(
                        f"Appending to existing speaker: {annotation.corrected_speaker}"
                    )
                    result = await speaker_client.append_to_speaker(
                        speaker_id=existing_speaker["id"], audio_data=wav_bytes
                    )

                    if "error" in result:
                        logger.error(f"Failed to append to speaker: {result}")
                        failed_count += 1
                        reason = f"Append failed: {result.get('error')}"
                        errors.append(reason)
                        await _record_training_failure(annotation, reason)
                        continue

                    appended_count += 1
                    logger.info(
                        f"✅ Successfully appended to speaker '{annotation.corrected_speaker}'"
                    )
                else:
                    # ENROLL new speaker
                    logger.info(
                        f"Enrolling new speaker: {annotation.corrected_speaker}"
                    )
                    result = await speaker_client.enroll_new_speaker(
                        speaker_name=annotation.corrected_speaker,
                        audio_data=wav_bytes,
                        user_id=1,  # TODO: Map Chronicle user_id to speaker service user_id
                    )

                    if "error" in result:
                        logger.error(f"Failed to enroll speaker: {result}")
                        failed_count += 1
                        reason = f"Enroll failed: {result.get('error')}"
                        errors.append(reason)
                        await _record_training_failure(annotation, reason)
                        continue

                    enrolled_count += 1
                    logger.info(
                        f"✅ Successfully enrolled new speaker '{annotation.corrected_speaker}'"
                    )

                # 4. Mark annotation as trained (clear any prior failure record)
                if annotation.processed_by:
                    annotation.processed_by = f"{annotation.processed_by},training"
                else:
                    annotation.processed_by = "training"
                annotation.training_error = None
                annotation.updated_at = datetime.now(timezone.utc)
                await annotation.save()

            except Exception as e:
                logger.error(
                    f"Error processing annotation {annotation.id}: {e}", exc_info=True
                )
                failed_count += 1
                reason = f"Exception: {str(e)[:50]}"
                errors.append(reason)
                try:
                    await _record_training_failure(annotation, reason)
                except Exception:
                    logger.error(
                        f"Failed to record training failure for {annotation.id}",
                        exc_info=True,
                    )
                continue

        total_processed = enrolled_count + appended_count
        logger.info(
            f"Training complete: {total_processed} processed "
            f"({enrolled_count} new, {appended_count} appended, {failed_count} failed)"
        )

        return JSONResponse(
            content={
                "message": "Training complete",
                "enrolled_new_speakers": enrolled_count,
                "appended_to_existing": appended_count,
                "total_processed": total_processed,
                "failed_count": failed_count,
                "errors": errors[:10] if errors else [],
                "status": "success" if total_processed > 0 else "partial_failure",
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing annotations for training: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process annotations for training: {str(e)}",
        )


@router.post("/export-asr-dataset")
async def export_asr_dataset(
    current_user: User = Depends(current_active_user),
):
    """
    Manually trigger ASR fine-tuning data export.

    Finds applied transcript/diarization annotations not yet consumed by ASR training,
    reconstructs audio, builds VibeVoice training labels, and POSTs to the ASR service.

    Returns:
        Export job results with counts of conversations exported and annotations consumed.
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="Only administrators can trigger ASR dataset export"
        )

    try:
        result = await run_asr_finetuning_job()
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"ASR dataset export failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"ASR dataset export failed: {str(e)}"
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
        # ------------------------------------------------------------------
        # Per-type annotation counts (with orphan detection)
        # ------------------------------------------------------------------
        annotation_counts: dict[str, dict] = {}
        trained_diarization_list: list = []
        failed_diarization_errors: list[str] = []

        # Collect all annotations to batch-check for orphans
        all_annotations_by_type: dict[AnnotationType, list] = {}
        for ann_type in AnnotationType:
            all_annotations_by_type[ann_type] = await Annotation.find(
                Annotation.annotation_type == ann_type,
            ).to_list()

        # Batch-check which conversation_ids still exist
        conv_annotation_types = {
            AnnotationType.DIARIZATION,
            AnnotationType.TRANSCRIPT,
            AnnotationType.SPEECH_SUGGESTION_CORRECTION,
        }
        all_conv_ids: set[str] = set()
        for ann_type in conv_annotation_types:
            for a in all_annotations_by_type.get(ann_type, []):
                if a.conversation_id:
                    all_conv_ids.add(a.conversation_id)

        existing_conv_ids: set[str] = set()
        if all_conv_ids:
            existing_convs = await Conversation.find(
                {"conversation_id": {"$in": list(all_conv_ids)}},
            ).to_list()
            existing_conv_ids = {c.conversation_id for c in existing_convs}

        orphaned_conv_ids = all_conv_ids - existing_conv_ids

        total_orphaned = 0
        for ann_type in AnnotationType:
            annotations = all_annotations_by_type[ann_type]

            # Identify orphaned annotations for conversation-based types
            if ann_type in conv_annotation_types:
                orphaned = [
                    a for a in annotations if a.conversation_id in orphaned_conv_ids
                ]
                non_orphaned = [
                    a for a in annotations if a.conversation_id not in orphaned_conv_ids
                ]
            else:
                # Memory/entity orphan detection is placeholder for now
                orphaned = []
                non_orphaned = annotations

            pending = [a for a in non_orphaned if not a.processed]
            processed = [a for a in non_orphaned if a.processed]
            trained = [
                a for a in processed if a.processed_by and "training" in a.processed_by
            ]
            applied_not_trained = [
                a
                for a in processed
                if not a.processed_by or "training" not in a.processed_by
            ]
            # "Failed" = applied annotations that hit a training error and are still
            # stuck (not yet trained). Surfaced so an admin can retry or discard them.
            failed = [a for a in applied_not_trained if (a.training_attempts or 0) > 0]

            orphan_count = len(orphaned)
            total_orphaned += orphan_count

            annotation_counts[ann_type.value] = {
                "total": len(non_orphaned),
                "pending": len(pending),
                "applied": len(applied_not_trained),
                "trained": len(trained),
                "orphaned": orphan_count,
                "failed": len(failed),
            }

            if ann_type == AnnotationType.DIARIZATION and failed:
                seen_errors: set[str] = set()
                for a in failed:
                    if a.training_error and a.training_error not in seen_errors:
                        seen_errors.add(a.training_error)
                        failed_diarization_errors.append(a.training_error)

            if ann_type == AnnotationType.DIARIZATION:
                trained_diarization_list = trained

        # ------------------------------------------------------------------
        # Diarization-specific fields (backward compat)
        # ------------------------------------------------------------------
        diarization = annotation_counts.get("diarization", {})
        pending_count = diarization.get("pending", 0)
        applied_count = diarization.get("applied", 0)
        trained_count = diarization.get("trained", 0)
        failed_count = diarization.get("failed", 0)

        # Get last training run timestamp from diarization annotations
        last_training_run = None
        if trained_diarization_list:
            latest_trained = max(
                trained_diarization_list,
                key=lambda a: (
                    a.updated_at
                    if a.updated_at
                    else datetime.min.replace(tzinfo=timezone.utc)
                ),
            )
            last_training_run = (
                latest_trained.updated_at.isoformat()
                if latest_trained.updated_at
                else None
            )

        # Get cron job status from scheduler
        try:
            scheduler = get_scheduler()
            all_jobs = await scheduler.get_all_jobs_status()
            # Find speaker finetuning job for backward compat
            speaker_job = next(
                (j for j in all_jobs if j["job_id"] == "speaker_finetuning"), None
            )
            cron_status = {
                "enabled": speaker_job["enabled"] if speaker_job else False,
                "schedule": speaker_job["schedule"] if speaker_job else "0 2 * * *",
                "last_run": speaker_job["last_run"] if speaker_job else None,
                "next_run": speaker_job["next_run"] if speaker_job else None,
            }
        except Exception:
            cron_status = {
                "enabled": False,
                "schedule": "0 2 * * *",
                "last_run": None,
                "next_run": None,
            }

        return JSONResponse(
            content={
                "pending_annotation_count": pending_count,
                "applied_annotation_count": applied_count,
                "trained_annotation_count": trained_count,
                "failed_annotation_count": failed_count,
                "failed_annotation_errors": failed_diarization_errors[:10],
                "last_training_run": last_training_run,
                "cron_status": cron_status,
                "annotation_counts": annotation_counts,
                "orphaned_annotation_count": total_orphaned,
            }
        )

    except Exception as e:
        logger.error(f"Error fetching fine-tuning status: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch fine-tuning status: {str(e)}",
        )


# ---------------------------------------------------------------------------
# Orphaned Annotation Management Endpoints
# ---------------------------------------------------------------------------


@router.delete("/orphaned-annotations")
async def delete_orphaned_annotations(
    current_user: User = Depends(current_active_user),
    annotation_type: Optional[str] = Query(
        None, description="Filter by annotation type (e.g. 'diarization')"
    ),
):
    """
    Find and delete orphaned annotations whose referenced conversation no longer exists.

    Only handles conversation-based annotation types (diarization, transcript).
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    conv_annotation_types = {AnnotationType.DIARIZATION, AnnotationType.TRANSCRIPT}

    # Filter to requested type if specified
    if annotation_type:
        try:
            requested_type = AnnotationType(annotation_type)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Unknown annotation type: {annotation_type}"
            )
        if requested_type not in conv_annotation_types:
            return JSONResponse(
                content={
                    "deleted_count": 0,
                    "by_type": {},
                    "message": "Orphan detection not supported for this type",
                }
            )
        types_to_check = {requested_type}
    else:
        types_to_check = conv_annotation_types

    # Collect all conversation_ids referenced by these annotation types
    all_conv_ids: set[str] = set()
    annotations_by_type: dict[AnnotationType, list] = {}
    for ann_type in types_to_check:
        annotations = await Annotation.find(
            Annotation.annotation_type == ann_type,
        ).to_list()
        annotations_by_type[ann_type] = annotations
        for a in annotations:
            if a.conversation_id:
                all_conv_ids.add(a.conversation_id)

    if not all_conv_ids:
        return JSONResponse(content={"deleted_count": 0, "by_type": {}})

    # Batch-check which conversations still exist
    existing_convs = await Conversation.find(
        {"conversation_id": {"$in": list(all_conv_ids)}},
    ).to_list()
    existing_conv_ids = {c.conversation_id for c in existing_convs}
    orphaned_conv_ids = all_conv_ids - existing_conv_ids

    if not orphaned_conv_ids:
        return JSONResponse(content={"deleted_count": 0, "by_type": {}})

    # Delete orphaned annotations
    deleted_by_type: dict[str, int] = {}
    total_deleted = 0
    for ann_type, annotations in annotations_by_type.items():
        orphaned = [a for a in annotations if a.conversation_id in orphaned_conv_ids]
        for a in orphaned:
            await a.delete()
        if orphaned:
            deleted_by_type[ann_type.value] = len(orphaned)
            total_deleted += len(orphaned)

    logger.info(f"Deleted {total_deleted} orphaned annotations: {deleted_by_type}")
    return JSONResponse(
        content={
            "deleted_count": total_deleted,
            "by_type": deleted_by_type,
        }
    )


@router.post("/orphaned-annotations/reattach")
async def reattach_orphaned_annotations(
    current_user: User = Depends(current_active_user),
):
    """Placeholder for reattaching orphaned annotations to a different conversation."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    raise HTTPException(status_code=501, detail="Reattach functionality coming soon")


# ---------------------------------------------------------------------------
# Failed Annotation Management Endpoints
# ---------------------------------------------------------------------------


async def _find_failed_annotations(annotation_type: Optional[str]) -> list[Annotation]:
    """Return applied-but-stuck annotations that have a recorded training failure.

    These are ``processed=True`` and not yet trained, with ``training_attempts > 0``.
    """
    if annotation_type:
        try:
            ann_type = AnnotationType(annotation_type)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Unknown annotation type: {annotation_type}"
            )
        candidates = await Annotation.find(
            Annotation.annotation_type == ann_type,
            Annotation.processed == True,
        ).to_list()
    else:
        candidates = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.processed == True,
        ).to_list()

    return [
        a
        for a in candidates
        if (a.training_attempts or 0) > 0
        and (not a.processed_by or "training" not in a.processed_by)
    ]


@router.post("/failed-annotations/retry")
async def retry_failed_annotations(
    current_user: User = Depends(current_active_user),
    annotation_type: Optional[str] = Query(
        None, description="Filter by annotation type (default: diarization)"
    ),
):
    """Clear the recorded failure on stuck annotations so they can be retried.

    Resets ``training_attempts``/``training_error`` (without deleting). The next
    training run re-attempts them; if the underlying cause is fixed they will
    succeed, otherwise they re-appear as failed.
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    failed = await _find_failed_annotations(annotation_type)
    for a in failed:
        a.training_attempts = 0
        a.training_error = None
        a.updated_at = datetime.now(timezone.utc)
        await a.save()

    logger.info(f"Reset {len(failed)} failed annotations for retry")
    return JSONResponse(content={"reset_count": len(failed)})


@router.delete("/failed-annotations")
async def delete_failed_annotations(
    current_user: User = Depends(current_active_user),
    annotation_type: Optional[str] = Query(
        None, description="Filter by annotation type (default: diarization)"
    ),
):
    """Discard stuck annotations that keep failing to train (corrupt/unusable)."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    failed = await _find_failed_annotations(annotation_type)
    for a in failed:
        await a.delete()

    logger.info(f"Deleted {len(failed)} failed annotations")
    return JSONResponse(content={"deleted_count": len(failed)})


# ---------------------------------------------------------------------------
# Cron Job Management Endpoints
# ---------------------------------------------------------------------------


class CronJobUpdate(BaseModel):
    enabled: Optional[bool] = None
    schedule: Optional[str] = None


@router.get("/cron-jobs")
async def get_cron_jobs(current_user: User = Depends(current_active_user)):
    """List all cron jobs with status, schedule, last/next run."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    scheduler = get_scheduler()
    return await scheduler.get_all_jobs_status()


@router.put("/cron-jobs/{job_id}")
async def update_cron_job(
    job_id: str,
    body: CronJobUpdate,
    current_user: User = Depends(current_active_user),
):
    """Update a cron job's schedule or enabled state."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    scheduler = get_scheduler()
    try:
        await scheduler.update_job(job_id, enabled=body.enabled, schedule=body.schedule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"message": f"Job '{job_id}' updated", "job_id": job_id}


@router.post("/cron-jobs/{job_id}/run")
async def run_cron_job_now(
    job_id: str,
    current_user: User = Depends(current_active_user),
):
    """Manually trigger a cron job."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    scheduler = get_scheduler()
    try:
        result = await scheduler.run_job_now(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return result
