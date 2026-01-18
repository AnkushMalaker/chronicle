"""
Speaker recognition related RQ job functions.

This module contains all jobs related to speaker identification and recognition.
"""

import asyncio
import logging
import time
from typing import Any, Dict

from advanced_omi_backend.auth import generate_jwt_for_user
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.services.audio_stream import (
    TranscriptionResultsAggregator,
)
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
from advanced_omi_backend.users import get_user_by_id

logger = logging.getLogger(__name__)


@async_job(redis=True, beanie=True)
async def check_enrolled_speakers_job(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    redis_client=None
) -> Dict[str, Any]:
    """
    Check if any enrolled speakers are present in the current audio stream.

    This job is used during speech detection to filter conversations by enrolled speakers.

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with enrolled_present, identified_speakers, and speaker_result
    """

    logger.info(f"🎤 Starting enrolled speaker check for session {session_id[:12]}")

    start_time = time.time()

    # Get aggregated transcription results
    aggregator = TranscriptionResultsAggregator(redis_client)
    raw_results = await aggregator.get_session_results(session_id)

    # Check for enrolled speakers
    speaker_client = SpeakerRecognitionClient()
    enrolled_present, speaker_result = await speaker_client.check_if_enrolled_speaker_present(
        redis_client=redis_client,
        client_id=client_id,
        session_id=session_id,
        user_id=user_id,
        transcription_results=raw_results
    )

    # Check for errors from speaker service
    if speaker_result and speaker_result.get("error"):
        error_type = speaker_result.get("error")
        error_message = speaker_result.get("message", "Unknown error")
        logger.error(f"🎤 [SPEAKER CHECK] Speaker service error: {error_type} - {error_message}")

        # For connection failures, assume no enrolled speakers but allow conversation to proceed
        # Speaker filtering is optional - if service is down, conversation should still be created
        if error_type in ("connection_failed", "timeout", "client_error"):
            logger.warning(
                f"⚠️ Speaker service unavailable ({error_type}), assuming no enrolled speakers. "
                f"Conversation will proceed normally."
            )
            return {
                "success": True,
                "session_id": session_id,
                "speaker_service_unavailable": True,
                "enrolled_present": False,
                "identified_speakers": [],
                "skip_reason": f"Speaker service unavailable: {error_type}",
                "processing_time_seconds": time.time() - start_time
            }

        # For other processing errors, also assume no enrolled speakers
        return {
            "success": False,
            "session_id": session_id,
            "error": f"Speaker recognition failed: {error_type}",
            "error_details": error_message,
            "enrolled_present": False,
            "identified_speakers": [],
            "processing_time_seconds": time.time() - start_time
        }

    # Extract identified speakers
    identified_speakers = []
    if speaker_result and "segments" in speaker_result:
        for seg in speaker_result["segments"]:
            identified_as = seg.get("identified_as")
            if identified_as and identified_as != "Unknown" and identified_as not in identified_speakers:
                identified_speakers.append(identified_as)

    processing_time = time.time() - start_time

    if enrolled_present:
        logger.info(f"✅ Enrolled speaker(s) found: {', '.join(identified_speakers)} ({processing_time:.2f}s)")
    else:
        logger.info(f"⏭️ No enrolled speakers found ({processing_time:.2f}s)")

    # Update job metadata for timeline tracking
    from rq import get_current_job
    current_job = get_current_job()
    if current_job:
        if not current_job.meta:
            current_job.meta = {}
        current_job.meta.update({
            "session_id": session_id,
            "client_id": client_id,
            "enrolled_present": enrolled_present,
            "identified_speakers": identified_speakers,
            "speaker_count": len(identified_speakers),
            "processing_time": processing_time
        })
        current_job.save_meta()

    return {
        "success": True,
        "session_id": session_id,
        "enrolled_present": enrolled_present,
        "identified_speakers": identified_speakers,
        "speaker_result": speaker_result,
        "processing_time_seconds": processing_time
    }


@async_job(redis=True, beanie=True)
async def recognise_speakers_job(
    conversation_id: str,
    version_id: str,
    transcript_text: str = "",
    words: list = None,
    *,
    redis_client=None
) -> Dict[str, Any]:
    """
    RQ job function for identifying speakers in a transcribed conversation.

    This job runs after transcription and:
    1. Reconstructs audio from MongoDB chunks
    2. Calls speaker recognition service to identify speakers
    3. Updates the transcript version with identified speaker labels
    4. Returns results for downstream jobs (memory)

    Args:
        conversation_id: Conversation ID
        version_id: Transcript version ID to update
        transcript_text: Transcript text from transcription job (optional, reads from DB if empty)
        words: Word-level timing data from transcription job (optional, reads from DB if empty)
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with processing results
    """

    logger.info(f"🎤 RQ: Starting speaker recognition for conversation {conversation_id}")

    start_time = time.time()

    # Get the conversation
    conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    # Get user_id from conversation
    user_id = conversation.user_id

    # Find the transcript version to update
    transcript_version = None
    for version in conversation.transcript_versions:
        if version.version_id == version_id:
            transcript_version = version
            break

    if not transcript_version:
        logger.error(f"Transcript version {version_id} not found")
        return {"success": False, "error": "Transcript version not found"}

    # Check if speaker recognition is enabled
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        logger.info(f"🎤 Speaker recognition disabled, skipping")
        return {
            "success": True,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "speaker_recognition_enabled": False,
            "processing_time_seconds": 0
        }

    # Read transcript text and words from the transcript version
    # (Parameters may be empty if called via job dependency)
    actual_transcript_text = transcript_text or transcript_version.transcript or ""
    actual_words = words if words else []

    # If words not provided, we need to get them from metadata
    if not actual_words and transcript_version.metadata:
        actual_words = transcript_version.metadata.get("words", [])

    if not actual_transcript_text:
        logger.warning(f"🎤 No transcript text found in version {version_id}")
        return {
            "success": False,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "error": "No transcript text available",
            "processing_time_seconds": 0
        }

    transcript_data = {
        "text": actual_transcript_text,
        "words": actual_words
    }

    # Generate backend token for speaker service to fetch audio
    # Speaker service will check conversation duration and decide
    # whether to chunk based on its own memory constraints

    # Get user details for token generation
    try:
        user = await get_user_by_id(user_id)
        if not user:
            logger.error(f"User {user_id} not found for token generation")
            return {
                "success": False,
                "conversation_id": conversation_id,
                "version_id": version_id,
                "error": "User not found",
                "processing_time_seconds": time.time() - start_time
            }

        backend_token = generate_jwt_for_user(user_id, user.email)
        logger.info(f"🔐 Generated backend token for speaker service")

    except Exception as token_error:
        logger.error(f"Failed to generate backend token: {token_error}", exc_info=True)
        return {
            "success": False,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "error": f"Token generation failed: {token_error}",
            "processing_time_seconds": time.time() - start_time
        }

    # Call speaker recognition service with conversation_id
    # Speaker service will:
    # 1. Fetch conversation metadata to check duration
    # 2. Decide whether to chunk based on its MAX_DIARIZE_DURATION setting
    # 3. Request audio segments via backend API as needed
    # 4. Return merged speaker segments
    logger.info(f"🎤 Calling speaker recognition service with conversation_id...")

    try:
        speaker_result = await speaker_client.diarize_identify_match(
            conversation_id=conversation_id,
            backend_token=backend_token,
            transcript_data=transcript_data,
            user_id=user_id
        )

        # Check for errors from speaker service
        if speaker_result.get("error"):
            error_type = speaker_result.get("error")
            error_message = speaker_result.get("message", "Unknown error")
            logger.error(f"🎤 Speaker recognition service error: {error_type} - {error_message}")

            # For connection failures, skip speaker recognition but allow downstream jobs to proceed
            # Speaker recognition is optional - memory extraction and other jobs should still run
            if error_type in ("connection_failed", "timeout", "client_error"):
                logger.warning(
                    f"⚠️ Speaker service unavailable ({error_type}), skipping speaker recognition. "
                    f"Downstream jobs (memory, title/summary, events) will proceed normally."
                )
                return {
                    "success": True,
                    "conversation_id": conversation_id,
                    "version_id": version_id,
                    "speaker_recognition_enabled": True,
                    "speaker_service_unavailable": True,
                    "identified_speakers": [],
                    "skip_reason": f"Speaker service unavailable: {error_type}",
                    "processing_time_seconds": time.time() - start_time
                }

            # For other errors (e.g., processing errors), return error dict without failing
            return {
                "success": False,
                "conversation_id": conversation_id,
                "version_id": version_id,
                "error": f"Speaker recognition failed: {error_type}",
                "error_details": error_message,
                "processing_time_seconds": time.time() - start_time
            }

        # Service worked but found no segments (legitimate empty result)
        if not speaker_result or "segments" not in speaker_result or not speaker_result["segments"]:
            logger.warning(f"🎤 Speaker recognition returned no segments")
            return {
                "success": True,
                "conversation_id": conversation_id,
                "version_id": version_id,
                "speaker_recognition_enabled": True,
                "identified_speakers": [],
                "processing_time_seconds": time.time() - start_time
            }

        speaker_segments = speaker_result["segments"]
        logger.info(f"🎤 Speaker recognition returned {len(speaker_segments)} segments")

        # Update the transcript version segments with identified speakers
        # Filter out empty segments (diarization sometimes creates segments with no text)
        updated_segments = []
        empty_segment_count = 0
        for seg in speaker_segments:
            # FIX: More robust empty segment detection
            text = seg.get("text", "").strip()

            # Skip segments with no text, whitespace-only, or very short
            if not text or len(text) < 3:
                empty_segment_count += 1
                logger.debug(f"Filtered empty/short segment: text='{text}'")
                continue

            # Skip segments with invalid structure
            if not isinstance(seg.get("start"), (int, float)) or not isinstance(seg.get("end"), (int, float)):
                empty_segment_count += 1
                logger.debug(f"Filtered segment with invalid timing: {seg}")
                continue

            speaker_name = seg.get("identified_as") or seg.get("speaker", "Unknown")
            updated_segments.append(
                Conversation.SpeakerSegment(
                    start=seg.get("start", 0),
                    end=seg.get("end", 0),
                    text=text,
                    speaker=speaker_name,
                    confidence=seg.get("confidence")
                )
            )

        if empty_segment_count > 0:
            logger.info(f"🔇 Filtered out {empty_segment_count} empty segments from speaker recognition")

        # Update the transcript version
        transcript_version.segments = updated_segments

        # Extract unique identified speakers for metadata
        identified_speakers = set()
        for seg in speaker_segments:
            identified_as = seg.get("identified_as", "Unknown")
            if identified_as != "Unknown":
                identified_speakers.add(identified_as)

        # Update metadata
        if not transcript_version.metadata:
            transcript_version.metadata = {}

        transcript_version.metadata["speaker_recognition"] = {
            "enabled": True,
            "identified_speakers": list(identified_speakers),
            "speaker_count": len(identified_speakers),
            "total_segments": len(speaker_segments),
            "processing_time_seconds": time.time() - start_time
        }

        await conversation.save()

        processing_time = time.time() - start_time
        logger.info(f"✅ Speaker recognition completed for {conversation_id} in {processing_time:.2f}s")

        return {
            "success": True,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "speaker_recognition_enabled": True,
            "identified_speakers": list(identified_speakers),
            "segment_count": len(updated_segments),
            "processing_time_seconds": processing_time
        }

    except Exception as speaker_error:
        logger.error(f"❌ Speaker recognition failed: {speaker_error}")
        import traceback
        logger.debug(traceback.format_exc())

        return {
            "success": False,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "error": str(speaker_error),
            "processing_time_seconds": time.time() - start_time
        }
