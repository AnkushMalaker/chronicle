"""
Speaker recognition related RQ job functions.

This module contains all jobs related to speaker identification and recognition.
"""

import asyncio
import logging
import time
from typing import Any, Dict

from advanced_omi_backend.auth import generate_jwt_for_user
from advanced_omi_backend.config import get_diarization_settings, get_misc_settings
from advanced_omi_backend.constants import UNKNOWN_SPEAKER_PREFIX
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.services.audio_stream import TranscriptionResultsAggregator
from advanced_omi_backend.services.forced_alignment import (
    synthesize_words_via_alignment,
)
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
from advanced_omi_backend.users import get_user_by_id
from advanced_omi_backend.utils.job_utils import update_job_meta

logger = logging.getLogger(__name__)


@async_job(redis=True, beanie=True)
async def check_enrolled_speakers_job(
    session_id: str, user_id: str, client_id: str, *, redis_client=None
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
    enrolled_present, speaker_result = (
        await speaker_client.check_if_enrolled_speaker_present(
            redis_client=redis_client,
            client_id=client_id,
            session_id=session_id,
            user_id=user_id,
            transcription_results=raw_results,
        )
    )

    # Check for errors from speaker service
    if speaker_result and speaker_result.get("error"):
        error_type = speaker_result.get("error")
        error_message = speaker_result.get("message", "Unknown error")
        logger.error(
            f"🎤 [SPEAKER CHECK] Speaker service error: {error_type} - {error_message}"
        )

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
                "processing_time_seconds": time.time() - start_time,
            }

        # For other processing errors, also assume no enrolled speakers
        return {
            "success": False,
            "session_id": session_id,
            "error": f"Speaker recognition failed: {error_type}",
            "error_details": error_message,
            "enrolled_present": False,
            "identified_speakers": [],
            "processing_time_seconds": time.time() - start_time,
        }

    # Extract identified speakers
    identified_speakers = []
    if speaker_result and "segments" in speaker_result:
        for seg in speaker_result["segments"]:
            identified_as = seg.get("identified_as")
            if (
                identified_as
                and identified_as != "Unknown"
                and identified_as not in identified_speakers
            ):
                identified_speakers.append(identified_as)

    processing_time = time.time() - start_time

    if enrolled_present:
        logger.info(
            f"✅ Enrolled speaker(s) found: {', '.join(identified_speakers)} ({processing_time:.2f}s)"
        )
    else:
        logger.info(f"⏭️ No enrolled speakers found ({processing_time:.2f}s)")

    # Update job metadata for timeline tracking
    update_job_meta(
        session_id=session_id,
        client_id=client_id,
        enrolled_present=enrolled_present,
        identified_speakers=identified_speakers,
        speaker_count=len(identified_speakers),
        processing_time=processing_time,
    )

    return {
        "success": True,
        "session_id": session_id,
        "enrolled_present": enrolled_present,
        "identified_speakers": identified_speakers,
        "speaker_result": speaker_result,
        "processing_time_seconds": processing_time,
    }


@async_job(redis=True, beanie=True)
async def recognise_speakers_job(
    conversation_id: str,
    version_id: str,
    transcript_text: str = "",
    words: list | None = None,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    RQ job function for identifying speakers in a transcribed conversation.

    This job adapts based on provider capabilities:
    1. If provider has diarization (e.g., VibeVoice) → skip pyannote, do identification only
    2. If provider has word timestamps (e.g., Parakeet) → full pyannote diarization + identification
    3. If no word timestamps → cannot run diarization, keep existing segments

    Speaker identification always runs if enrolled speakers exist, mapping
    generic labels ("Speaker 0") to enrolled speaker names ("Alice").

    Args:
        conversation_id: Conversation ID
        version_id: Transcript version ID to update
        transcript_text: Transcript text from transcription job (optional, reads from DB if empty)
        words: Word-level timing data from transcription job (optional, reads from DB if empty)
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with processing results
    """

    logger.info(
        f"🎤 RQ: Starting speaker recognition for conversation {conversation_id}"
    )

    start_time = time.time()

    # Get the conversation
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    # Get user_id from conversation
    user_id = conversation.user_id

    # Resolve the transcript version to update. The handed-in version_id is the one
    # the upstream transcription step produced and set active. When that step declined
    # to create a new version (e.g. an empty/contentless batch re-transcription that
    # kept the existing streaming transcript), version_id won't exist — so fall back to
    # whatever transcript is currently active, which is exactly what upstream kept.
    transcript_version = conversation.get_transcript_version(version_id)
    if not transcript_version:
        transcript_version = conversation.active_transcript
        if transcript_version:
            logger.warning(
                f"Transcript version {version_id} not found; falling back to active "
                f"version {transcript_version.version_id} (upstream likely kept the "
                f"existing transcript)"
            )
            version_id = transcript_version.version_id
        else:
            logger.error(
                f"Transcript version {version_id} not found and no active version exists"
            )
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
            "processing_time_seconds": 0,
        }

    # Get provider capabilities from metadata
    provider_capabilities = transcript_version.metadata.get("provider_capabilities", {})
    provider_has_diarization = provider_capabilities.get("diarization", False)
    provider_has_word_timestamps = provider_capabilities.get("word_timestamps", False)

    # Check if provider already did diarization (set by transcription job)
    diarization_source = transcript_version.diarization_source
    provider_diarized = provider_has_diarization or diarization_source == "provider"

    # Diarization policy from settings:
    # - "provider": trust provider diarization when available, fall back to pyannote
    # - "pyannote": always re-diarize with pyannote (when word timestamps allow it)
    diarization_settings = get_diarization_settings()
    preferred_source = diarization_settings.get("diarization_source", "provider")
    use_provider_diarization = provider_diarized and preferred_source == "provider"

    if use_provider_diarization:
        # Provider already did diarization (e.g., VibeVoice, Deepgram batch)
        # Skip pyannote diarization, go straight to speaker identification
        logger.info(
            f"🎤 Provider already diarized (diarization_source={diarization_source}), "
            f"skipping pyannote diarization - will run speaker identification only"
        )

        # If we have existing segments from provider, proceed to identification
        if transcript_version.segments:
            logger.info(
                f"🎤 Using {len(transcript_version.segments)} segments from provider"
            )
            # Continue to speaker identification below (after this block)
        else:
            logger.warning(f"🎤 Provider claimed diarization but no segments found")
            # Still continue - identification may work with audio analysis

    # Read transcript text and words from the transcript version
    # (Parameters may be empty if called via job dependency)
    actual_transcript_text = transcript_text or transcript_version.transcript or ""
    actual_words = words if words else []

    # If words not provided as parameter, read from version.words field (standardized location)
    if not actual_words and transcript_version.words:
        # Convert Word objects to dicts for speaker service API
        actual_words = [
            {"word": w.word, "start": w.start, "end": w.end, "confidence": w.confidence}
            for w in transcript_version.words
        ]
        logger.info(
            f"🔤 Loaded {len(actual_words)} words from transcript version.words field"
        )
    # Backward compatibility: Fall back to metadata if words field is empty (old data)
    elif not actual_words and transcript_version.metadata.get("words"):
        actual_words = transcript_version.metadata.get("words", [])
        logger.info(
            f"🔤 Loaded {len(actual_words)} words from transcript version metadata (legacy)"
        )
    # Backward compatibility: Extract from segments if that's all we have (old streaming data)
    elif not actual_words and transcript_version.segments:
        for segment in transcript_version.segments:
            if segment.words:
                for w in segment.words:
                    actual_words.append(
                        {
                            "word": w.word,
                            "start": w.start,
                            "end": w.end,
                            "confidence": w.confidence,
                        }
                    )
        if actual_words:
            logger.info(
                f"🔤 Extracted {len(actual_words)} words from segments (legacy)"
            )

    if not actual_transcript_text:
        logger.warning(f"🎤 No transcript text found in version {version_id}")
        return {
            "success": False,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "error": "No transcript text available",
            "processing_time_seconds": 0,
        }

    # If the provider gave segment text but no word timestamps (e.g. VibeVoice),
    # synthesize word timings via forced alignment so we can run the full pyannote
    # re-diarization path (Path A) instead of per-segment identification on the
    # provider's segments. This unifies word-less conversations onto the better path.
    if (
        not actual_words
        and not use_provider_diarization
        and transcript_version.segments
    ):
        speech_for_align = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in transcript_version.segments
            if getattr(s, "segment_type", "speech") == "speech"
            and (s.text or "").strip()
        ]
        total_dur = max((s.end for s in transcript_version.segments), default=0.0)
        if speech_for_align and total_dur > 0:
            logger.info(
                f"🔤 No word timestamps; forced-aligning {len(speech_for_align)} "
                f"segments to recover word timing for re-diarization"
            )
            actual_words = await synthesize_words_via_alignment(
                conversation_id, speech_for_align, total_dur
            )

    # Check if we can run pyannote diarization
    # Pyannote requires word timestamps to align speaker segments with text
    can_run_pyannote = bool(actual_words) and not use_provider_diarization

    if not actual_words and not provider_diarized:
        if not transcript_version.segments:
            # No words, no provider diarization, no existing segments - nothing we can do
            logger.warning(
                f"🎤 No word timestamps available, provider didn't diarize, "
                f"and no existing segments to identify."
            )
            return {
                "success": False,
                "conversation_id": conversation_id,
                "version_id": version_id,
                "error": "No word timestamps and no segments available",
                "processing_time_seconds": time.time() - start_time,
            }
        # Has existing segments - fall through to run identification on them
        logger.info(
            f"🎤 No word timestamps for pyannote re-diarization, but "
            f"{len(transcript_version.segments)} existing segments found. "
            f"Running speaker identification on existing segments."
        )

    # Per-segment identification mode comes solely from settings
    misc_config = get_misc_settings()
    use_per_segment = misc_config.get("per_segment_speaker_id", False)
    if use_per_segment:
        logger.info("🎤 Per-segment identification mode active (config toggle enabled)")

    try:
        ran_pyannote_diarization = False
        if transcript_version.segments and not can_run_pyannote:
            # Have existing segments and can't/shouldn't run pyannote - do identification only
            # Covers: provider already diarized, no word timestamps but segments exist, etc.
            # Only send speech segments for identification; skip event/note segments
            speech_segments = [
                s
                for s in transcript_version.segments
                if getattr(s, "segment_type", "speech") == "speech"
            ]
            logger.info(
                f"🎤 Using segment-level speaker identification on {len(speech_segments)} speech segments "
                f"(skipped {len(transcript_version.segments) - len(speech_segments)} non-speech)"
            )
            segments_data = [
                {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
                for s in speech_segments
            ]
            speaker_result = await speaker_client.identify_provider_segments(
                conversation_id=conversation_id,
                segments=segments_data,
                user_id=user_id,
                per_segment=use_per_segment,
                min_segment_duration=0.5 if use_per_segment else 1.5,
            )
        else:
            # Standard path: full diarization + identification via speaker service
            ran_pyannote_diarization = True
            transcript_data = {"text": actual_transcript_text, "words": actual_words}

            # Generate backend token for speaker service to fetch audio
            try:
                user = await get_user_by_id(user_id)
                if not user:
                    logger.error(f"User {user_id} not found for token generation")
                    return {
                        "success": False,
                        "conversation_id": conversation_id,
                        "version_id": version_id,
                        "error": "User not found",
                        "processing_time_seconds": time.time() - start_time,
                    }

                backend_token = generate_jwt_for_user(user_id, user.email)
                logger.info(f"🔐 Generated backend token for speaker service")

            except Exception as token_error:
                logger.error(
                    f"Failed to generate backend token: {token_error}", exc_info=True
                )
                return {
                    "success": False,
                    "conversation_id": conversation_id,
                    "version_id": version_id,
                    "error": f"Token generation failed: {token_error}",
                    "processing_time_seconds": time.time() - start_time,
                }

            logger.info(
                f"🎤 Calling speaker recognition service with conversation_id..."
            )
            speaker_result = await speaker_client.diarize_identify_match(
                conversation_id=conversation_id,
                backend_token=backend_token,
                transcript_data=transcript_data,
                user_id=user_id,
            )

        # Check for errors from speaker service
        if speaker_result.get("error"):
            error_type = speaker_result.get("error")
            error_message = speaker_result.get("message", "Unknown error")
            logger.error(
                f"🎤 Speaker recognition service error: {error_type} - {error_message}"
            )

            # Connection/timeout errors → skip gracefully (existing behavior)
            if error_type in ("connection_failed", "timeout", "client_error"):
                logger.warning(
                    f"⚠️ Speaker service unavailable ({error_type}), skipping speaker recognition. "
                    f"Downstream jobs (memory, title/summary, events) will proceed normally."
                )
                return {
                    "success": True,  # Allow pipeline to continue
                    "conversation_id": conversation_id,
                    "version_id": version_id,
                    "speaker_recognition_enabled": True,
                    "speaker_service_unavailable": True,
                    "identified_speakers": [],
                    "skip_reason": f"Speaker service unavailable: {error_type}",
                    "error_type": error_type,
                    "processing_time_seconds": time.time() - start_time,
                }

            # Validation errors → fail job, don't retry
            elif error_type == "validation_error":
                logger.error(f"❌ Speaker service validation error: {error_message}")
                return {
                    "success": False,
                    "conversation_id": conversation_id,
                    "version_id": version_id,
                    "error": f"Validation error: {error_message}",
                    "error_type": error_type,
                    "retryable": False,  # Don't retry validation errors
                    "processing_time_seconds": time.time() - start_time,
                }

            # Resource errors → fail job, can retry later
            elif error_type == "resource_error":
                logger.error(f"❌ Speaker service resource error: {error_message}")
                return {
                    "success": False,
                    "conversation_id": conversation_id,
                    "version_id": version_id,
                    "error": f"Resource error: {error_message}",
                    "error_type": error_type,
                    "retryable": True,  # Can retry later when resources available
                    "processing_time_seconds": time.time() - start_time,
                }

            # Unknown errors → fail job
            else:
                return {
                    "success": False,
                    "conversation_id": conversation_id,
                    "version_id": version_id,
                    "error": f"Speaker recognition failed: {error_type}",
                    "error_details": error_message,
                    "error_type": error_type,
                    "processing_time_seconds": time.time() - start_time,
                }

        # Service worked but found no segments (legitimate empty result)
        if (
            not speaker_result
            or "segments" not in speaker_result
            or not speaker_result["segments"]
        ):
            logger.warning(f"🎤 Speaker recognition returned no segments")
            return {
                "success": True,
                "conversation_id": conversation_id,
                "version_id": version_id,
                "speaker_recognition_enabled": True,
                "identified_speakers": [],
                "processing_time_seconds": time.time() - start_time,
            }

        speaker_segments = speaker_result["segments"]
        logger.info(f"🎤 Speaker recognition returned {len(speaker_segments)} segments")

        # Build mapping for unknown speakers: diarization_label -> "Unknown Speaker N"
        unknown_label_map = {}
        unknown_counter = 1
        for seg in speaker_segments:
            identified_as = seg.get("identified_as")
            if not identified_as:
                label = seg.get("speaker", "Unknown")
                if label not in unknown_label_map:
                    unknown_label_map[label] = (
                        f"{UNKNOWN_SPEAKER_PREFIX} {unknown_counter}"
                    )
                    unknown_counter += 1

        if unknown_label_map:
            logger.info(f"🎤 Unknown speaker mapping: {unknown_label_map}")

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
            if not isinstance(seg.get("start"), (int, float)) or not isinstance(
                seg.get("end"), (int, float)
            ):
                empty_segment_count += 1
                logger.debug(f"Filtered segment with invalid timing: {seg}")
                continue

            speaker_name = seg.get("identified_as") or unknown_label_map.get(
                seg.get("speaker", "Unknown"), UNKNOWN_SPEAKER_PREFIX
            )

            # Extract words from speaker service response (already matched to this segment)
            words_data = seg.get("words", [])
            segment_words = [
                Conversation.Word(
                    word=w.get("word", ""),
                    start=w.get("start", 0.0),
                    end=w.get("end", 0.0),
                    confidence=w.get("confidence"),
                )
                for w in words_data
            ]

            # Classify segment type from content
            from advanced_omi_backend.utils.segment_utils import classify_segment_text

            seg_classification = classify_segment_text(text)
            seg_type = "event" if seg_classification == "event" else "speech"

            updated_segments.append(
                Conversation.SpeakerSegment(
                    start=seg.get("start", 0),
                    end=seg.get("end", 0),
                    text=text,
                    speaker="" if seg_type == "event" else speaker_name,
                    segment_type=seg_type,
                    identified_as=seg.get("identified_as"),
                    confidence=seg.get("confidence"),
                    words=segment_words,  # Use words from speaker service
                )
            )

        if empty_segment_count > 0:
            logger.info(
                f"🔇 Filtered out {empty_segment_count} empty segments from speaker recognition"
            )

        # Re-insert non-speech segments (event/note) that were skipped during identification
        # They need to be merged back into position based on timestamps
        non_speech_segments = [
            s
            for s in transcript_version.segments
            if getattr(s, "segment_type", "speech") != "speech"
        ]
        if non_speech_segments:
            for ns_seg in non_speech_segments:
                # Find correct insertion position based on start time
                insert_pos = len(updated_segments)
                for i, seg in enumerate(updated_segments):
                    if seg.start > ns_seg.start:
                        insert_pos = i
                        break
                updated_segments.insert(insert_pos, ns_seg)
            logger.info(
                f"🎤 Re-inserted {len(non_speech_segments)} non-speech segments"
            )

        # Update the transcript version
        transcript_version.segments = updated_segments

        # Extract unique identified speakers for metadata
        identified_speakers = set()
        for seg in speaker_segments:
            identified_as = seg.get("identified_as")
            if identified_as and identified_as != "Unknown":
                identified_speakers.add(identified_as)

        # Update metadata
        if not transcript_version.metadata:
            transcript_version.metadata = {}

        sr_metadata = {
            "enabled": True,
            "identification_mode": (
                "per_segment" if use_per_segment else "majority_vote"
            ),
            "identified_speakers": list(identified_speakers),
            "speaker_count": len(identified_speakers),
            "total_segments": len(speaker_segments),
            "processing_time_seconds": time.time() - start_time,
        }
        if speaker_result.get("partial_errors"):
            sr_metadata["partial_errors"] = speaker_result["partial_errors"]
        transcript_version.metadata["speaker_recognition"] = sr_metadata

        # Record which engine produced the speaker segments on this version
        if ran_pyannote_diarization:
            transcript_version.diarization_source = "pyannote"

        await conversation.save()

        processing_time = time.time() - start_time
        logger.info(
            f"✅ Speaker recognition completed for {conversation_id} in {processing_time:.2f}s"
        )

        return {
            "success": True,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "speaker_recognition_enabled": True,
            "identified_speakers": list(identified_speakers),
            "segment_count": len(updated_segments),
            "processing_time_seconds": processing_time,
        }

    except asyncio.TimeoutError as e:
        logger.error(f"❌ Speaker recognition timeout: {e}")

        # Add timeout metadata to job
        update_job_meta(
            error_type="timeout",
            audio_duration=conversation.audio_total_duration if conversation else None,
            timeout_occurred_at=time.time(),
        )

        return {
            "success": False,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "error": "Speaker recognition timeout",
            "error_type": "timeout",
            "audio_duration": (
                conversation.audio_total_duration if conversation else None
            ),
            "processing_time_seconds": time.time() - start_time,
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
            "processing_time_seconds": time.time() - start_time,
        }
