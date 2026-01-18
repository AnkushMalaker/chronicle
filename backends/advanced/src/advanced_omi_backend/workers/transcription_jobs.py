"""
Transcription-related RQ job functions.

This module contains all jobs related to speech-to-text transcription processing.
"""

import asyncio
import os
import logging
import time
from typing import Dict, Any
from rq.exceptions import NoSuchJobError

from advanced_omi_backend.models.job import JobPriority, BaseRQJob, async_job

from advanced_omi_backend.controllers.queue_controller import (
    transcription_queue,
    redis_conn,
    JOB_RESULT_TTL,
    REDIS_URL,
)
from advanced_omi_backend.utils.conversation_utils import analyze_speech, mark_conversation_deleted
from advanced_omi_backend.services.plugin_service import get_plugin_router

logger = logging.getLogger(__name__)


async def apply_speaker_recognition(
    audio_path: str,
    transcript_text: str,
    words: list,
    segments: list,
    user_id: str,
    conversation_id: str = None,
) -> list:
    """
    Apply speaker recognition to segments using the speaker recognition service.

    This is a reusable helper function that can be called from any job.

    Args:
        audio_path: Path to the audio file
        transcript_text: Full transcript text
        words: Word-level timing data
        segments: List of Conversation.SpeakerSegment objects
        user_id: User ID
        conversation_id: Optional conversation ID for logging

    Returns:
        Updated list of segments with identified speakers
    """
    try:
        from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient

        speaker_client = SpeakerRecognitionClient()
        if not speaker_client.enabled:
            logger.info(f"🎤 Speaker recognition disabled, using original speaker labels")
            return segments

        logger.info(
            f"🎤 Speaker recognition enabled, identifying speakers{f' for {conversation_id}' if conversation_id else ''}..."
        )

        # Prepare transcript data with word-level timings
        transcript_data = {"text": transcript_text, "words": words}

        # Call speaker recognition service to match and identify speakers
        speaker_result = await speaker_client.diarize_identify_match(
            audio_path=audio_path, transcript_data=transcript_data, user_id=user_id
        )

        if not speaker_result or "segments" not in speaker_result:
            logger.info(
                f"🎤 Speaker recognition returned no segments, keeping original transcription segments"
            )
            return segments

        speaker_identified_segments = speaker_result["segments"]
        logger.info(
            f"🎤 Speaker recognition returned {len(speaker_identified_segments)} identified segments"
        )
        logger.info(f"🎤 Original segments: {len(segments)}")

        # Create time-based speaker mapping
        def get_speaker_at_time(timestamp: float, speaker_segments: list) -> str:
            """Get the identified speaker active at a given timestamp."""
            for seg in speaker_segments:
                seg_start = seg.get("start", 0.0)
                seg_end = seg.get("end", 0.0)
                if seg_start <= timestamp <= seg_end:
                    return seg.get("identified_as") or seg.get("speaker", "Unknown")
            return None

        # Update each segment's speaker based on its timestamp
        updated_count = 0
        for seg in segments:
            seg_mid = (seg.start + seg.end) / 2.0
            identified_speaker = get_speaker_at_time(seg_mid, speaker_identified_segments)

            if identified_speaker and identified_speaker != "Unknown":
                original_speaker = seg.speaker
                seg.speaker = identified_speaker
                updated_count += 1
                logger.debug(
                    f"🎤   Segment [{seg.start:.1f}-{seg.end:.1f}] '{original_speaker}' -> '{identified_speaker}'"
                )

        # Ensure segments remain sorted by start time
        segments.sort(key=lambda s: s.start)
        logger.info(
            f"🎤 Updated {updated_count}/{len(segments)} segments with speaker identifications"
        )

        return segments

    except Exception as speaker_error:
        logger.warning(f"⚠️ Speaker recognition failed: {speaker_error}")
        logger.warning(f"Continuing with original transcription speaker labels")
        import traceback

        logger.debug(traceback.format_exc())
        return segments


@async_job(redis=True, beanie=True)
async def transcribe_full_audio_job(
    conversation_id: str,
    version_id: str,
    trigger: str = "reprocess",
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    RQ job function for transcribing full audio to text (transcription only, no speaker recognition).

    This job:
    1. Reconstructs audio from MongoDB chunks
    2. Transcribes audio to text with generic speaker labels (Speaker 0, Speaker 1, etc.)
    3. Generates title and summary
    4. Saves transcript version to conversation
    5. Returns results for downstream jobs (speaker recognition, memory)

    Speaker recognition is handled by a separate job (recognise_speakers_job).

    Args:
        conversation_id: Conversation ID
        version_id: Version ID for new transcript
        trigger: Trigger source
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with processing results including transcript data for next job
    """
    from pathlib import Path
    from advanced_omi_backend.services.transcription import get_transcription_provider
    from advanced_omi_backend.models.conversation import Conversation

    logger.info(
        f"🔄 RQ: Starting transcript processing for conversation {conversation_id} (trigger: {trigger})"
    )

    start_time = time.time()

    # Get the conversation
    conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if not conversation:
        raise ValueError(f"Conversation {conversation_id} not found")

    # Extract user_id and client_id for plugin context
    user_id = str(conversation.user_id) if conversation.user_id else None
    client_id = conversation.client_id if hasattr(conversation, 'client_id') else None

    # Get the transcription provider
    provider = get_transcription_provider(mode="batch")
    if not provider:
        raise ValueError("No transcription provider available")

    provider_name = provider.name
    logger.info(f"Using transcription provider: {provider_name}")

    # Reconstruct audio from MongoDB chunks
    from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_wav_from_conversation

    logger.info(f"📦 Reconstructing audio from MongoDB chunks for conversation {conversation_id}")

    try:
        # Reconstruct WAV from MongoDB chunks (already in memory as bytes)
        wav_data = await reconstruct_wav_from_conversation(conversation_id)

        logger.info(
            f"📦 Reconstructed audio from MongoDB chunks: "
            f"{len(wav_data) / 1024 / 1024:.2f} MB"
        )

        # Transcribe the audio directly from memory (no disk I/O needed)
        transcription_result = await provider.transcribe(
            audio_data=wav_data,  # Pass bytes directly, already in memory
            sample_rate=16000,
            diarize=True
        )

    except ValueError as e:
        # No chunks found for conversation
        raise FileNotFoundError(f"No audio chunks found for conversation {conversation_id}: {e}")
    except Exception as e:
        logger.error(f"Failed to reconstruct audio from MongoDB: {e}", exc_info=True)
        raise RuntimeError(f"Audio reconstruction failed: {e}")

    # Extract results
    transcript_text = transcription_result.get("text", "")
    segments = transcription_result.get("segments", [])
    words = transcription_result.get("words", [])

    logger.info(
        f"📊 Transcription complete: {len(transcript_text)} chars, {len(segments)} segments, {len(words)} words"
    )

    # Trigger transcript-level plugins BEFORE speech validation
    # This ensures wake-word commands execute even if conversation gets deleted
    logger.info(f"🔍 DEBUG: About to trigger plugins - transcript_text exists: {bool(transcript_text)}")
    if transcript_text:
        try:
            from advanced_omi_backend.services.plugin_service import init_plugin_router

            # Initialize plugin router if not already initialized (worker context)
            plugin_router = get_plugin_router()
            logger.info(f"🔍 DEBUG: Plugin router from service: {plugin_router is not None}")

            if not plugin_router:
                logger.info("🔧 Initializing plugin router in worker process...")
                plugin_router = init_plugin_router()
                logger.info(f"🔧 After init, plugin_router: {plugin_router is not None}, plugins count: {len(plugin_router.plugins) if plugin_router else 0}")

                # Initialize async plugins
                if plugin_router:
                    for plugin_id, plugin in plugin_router.plugins.items():
                        try:
                            await plugin.initialize()
                            logger.info(f"✅ Plugin '{plugin_id}' initialized in worker")
                        except Exception as e:
                            logger.exception(f"Failed to initialize plugin '{plugin_id}' in worker: {e}")

            logger.info(f"🔍 DEBUG: Plugin router final check: {plugin_router is not None}, has {len(plugin_router.plugins) if plugin_router else 0} plugins")

            if plugin_router:
                logger.info(f"🔍 DEBUG: Preparing to trigger transcript plugins for conversation {conversation_id}")
                plugin_data = {
                    'transcript': transcript_text,
                    'segment_id': f"{conversation_id}_batch",
                    'conversation_id': conversation_id,
                    'segments': segments,
                    'word_count': len(words),
                }

                logger.info(f"🔍 DEBUG: Dispatching transcript.batch event with user_id={user_id}, client_id={client_id}")
                plugin_results = await plugin_router.dispatch_event(
                    event='transcript.batch',
                    user_id=user_id,
                    data=plugin_data,
                    metadata={'client_id': client_id}
                )
                logger.info(f"🔍 DEBUG: Event dispatch returned {len(plugin_results) if plugin_results else 0} results")

                if plugin_results:
                    logger.info(f"✅ Triggered {len(plugin_results)} transcript plugins in batch mode")
                    for result in plugin_results:
                        if result.message:
                            logger.info(f"  Plugin: {result.message}")
        except Exception as e:
            logger.exception(f"⚠️ Error triggering transcript plugins in batch mode: {e}")

    logger.info(f"🔍 DEBUG: Plugin processing complete, moving to speech validation")

    # Validate meaningful speech BEFORE any further processing
    transcript_data = {"text": transcript_text, "words": words}
    speech_analysis = analyze_speech(transcript_data)

    if not speech_analysis.get("has_speech", False):
        logger.warning(
            f"⚠️ Transcription found no meaningful speech for conversation {conversation_id}: "
            f"{speech_analysis.get('reason', 'unknown')}"
        )

        # Mark conversation as deleted
        await mark_conversation_deleted(
            conversation_id=conversation_id,
            deletion_reason="no_meaningful_speech_batch_transcription",
        )

        # Cancel all dependent jobs (cropping, speaker recognition, memory, title/summary)
        from rq import get_current_job
        from rq.job import Job

        current_job = get_current_job()
        if current_job:
            # Get all jobs that depend on this transcription job
            from advanced_omi_backend.controllers.queue_controller import redis_conn

            # Find dependent jobs by searching for jobs with this job as dependency
            try:
                # Cancel jobs based on conversation_id pattern
                job_patterns = [
                    f"crop_{conversation_id[:12]}",
                    f"speaker_{conversation_id[:12]}",
                    f"memory_{conversation_id[:12]}",
                    f"title_summary_{conversation_id[:12]}",
                ]

                cancelled_jobs = []
                for job_id in job_patterns:
                    try:
                        dependent_job = Job.fetch(job_id, connection=redis_conn)
                        if dependent_job and dependent_job.get_status() in [
                            "queued",
                            "deferred",
                            "scheduled",
                        ]:
                            dependent_job.cancel()
                            cancelled_jobs.append(job_id)
                            logger.info(f"✅ Cancelled dependent job: {job_id}")
                    except Exception as e:
                        if isinstance(e, NoSuchJobError):
                            logger.debug(f"Job {job_id} hash not found (likely already completed or expired)")
                        else:
                            logger.debug(f"Job {job_id} not found or already completed: {e}")

                if cancelled_jobs:
                    logger.info(
                        f"🚫 Cancelled {len(cancelled_jobs)} dependent jobs due to no meaningful speech"
                    )
            except Exception as cancel_error:
                logger.warning(f"Failed to cancel some dependent jobs: {cancel_error}")

        # Return early with failure status
        return {
            "success": False,
            "conversation_id": conversation_id,
            "error": "no_meaningful_speech",
            "reason": speech_analysis.get("reason"),
            "word_count": speech_analysis.get("word_count", 0),
            "duration": speech_analysis.get("duration", 0.0),
            "deleted": True,
        }

    logger.info(
        f"✅ Meaningful speech validated: {speech_analysis.get('word_count')} words, "
        f"{speech_analysis.get('duration', 0):.1f}s"
    )

    # Calculate processing time (transcription only)
    processing_time = time.time() - start_time

    # Convert segments to SpeakerSegment objects
    speaker_segments = []

    if segments:
        # Use provided segments
        for seg in segments:
            # Use identified_as if available (from speaker recognition), otherwise use speaker label
            speaker_id = seg.get("identified_as") or seg.get("speaker", "Unknown")
            # Convert speaker ID to string if it's an integer (some providers return int speaker IDs)
            speaker_name = f"Speaker {speaker_id}" if isinstance(speaker_id, int) else speaker_id

            speaker_segments.append(
                Conversation.SpeakerSegment(
                    start=seg.get("start", 0),
                    end=seg.get("end", 0),
                    text=seg.get("text", ""),
                    speaker=speaker_name,
                    confidence=seg.get("confidence"),
                )
            )
    elif transcript_text:
        # Fallback: If no segments but we have text, create a single segment from the full transcript
        # This handles providers that don't support segmentation
        # Calculate duration from words if available, otherwise estimate from audio
        start_time_seg = 0.0
        end_time_seg = 0.0

        if words:
            # Use word timestamps if available
            start_times = [w.get("start", 0) for w in words if "start" in w]
            end_times = [w.get("end", 0) for w in words if "end" in w]
            if start_times:
                start_time_seg = min(start_times)
            if end_times:
                end_time_seg = max(end_times)
        else:
            # Estimate duration: assume ~150 words per minute, or use audio file duration
            # For now, use a default duration if we can't calculate it
            end_time_seg = len(transcript_text.split()) * 0.4  # Rough estimate: 0.4s per word

        speaker_segments.append(
            Conversation.SpeakerSegment(
                start=start_time_seg,
                end=end_time_seg if end_time_seg > start_time_seg else start_time_seg + 1.0,
                text=transcript_text,
                speaker="Unknown",
                confidence=None,
            )
        )
        logger.info(
            f"📊 Created single segment from transcript text (no segments returned by provider)"
        )

    logger.info(f"📊 Created {len(speaker_segments)} speaker segments")

    # Add new transcript version
    provider_normalized = provider_name.lower() if provider_name else "unknown"

    # Prepare metadata (transcription only - speaker recognition will add its own metadata)
    metadata = {
        "trigger": trigger,
        "audio_file_size": len(wav_data),
        "segment_count": len(segments),
        "word_count": len(words),
        "words": words,  # Store words for speaker recognition job to read
        "speaker_recognition": {"enabled": False, "reason": "handled_by_separate_job"},
    }

    conversation.add_transcript_version(
        version_id=version_id,
        transcript=transcript_text,
        segments=speaker_segments,
        provider=provider_normalized,  # Now just a string, no enum constructor needed
        model=provider.name,
        processing_time_seconds=processing_time,
        metadata=metadata,
        set_as_active=True,
    )

    # Generate title and summary from transcript using LLM
    if transcript_text and len(transcript_text.strip()) > 0:
        try:
            from advanced_omi_backend.llm_client import async_generate

            # Prepare prompt for LLM
            prompt = f"""Based on this conversation transcript, generate a concise title and summary.

Transcript:
{transcript_text[:2000]}

Respond in this exact format:
Title: <concise title under 50 characters>
Summary: <brief summary under 150 characters>"""

            logger.info(f"🤖 Generating title/summary using LLM for conversation {conversation_id}")
            llm_response = await async_generate(prompt, temperature=0.7)

            # Parse LLM response
            lines = llm_response.strip().split("\n")
            title = None
            summary = None

            for line in lines:
                if line.startswith("Title:"):
                    title = line.replace("Title:", "").strip()
                elif line.startswith("Summary:"):
                    summary = line.replace("Summary:", "").strip()

            # Use LLM-generated title/summary if valid, otherwise fallback
            if title and len(title) > 0:
                conversation.title = title[:50] + "..." if len(title) > 50 else title
            else:
                # Fallback to first sentence if LLM didn't provide title
                first_sentence = transcript_text.split(".")[0].strip()
                conversation.title = (
                    first_sentence[:50] + "..." if len(first_sentence) > 50 else first_sentence
                )

            if summary and len(summary) > 0:
                conversation.summary = summary[:150] + "..." if len(summary) > 150 else summary
            else:
                # Fallback to truncated transcript if LLM didn't provide summary
                conversation.summary = (
                    transcript_text[:150] + "..." if len(transcript_text) > 150 else transcript_text
                )

            logger.info(
                f"✅ Generated title: '{conversation.title}', summary: '{conversation.summary}'"
            )

        except Exception as llm_error:
            logger.warning(f"⚠️ LLM title/summary generation failed: {llm_error}")
            # Fallback to simple truncation
            first_sentence = transcript_text.split(".")[0].strip()
            conversation.title = (
                first_sentence[:50] + "..." if len(first_sentence) > 50 else first_sentence
            )
            conversation.summary = (
                transcript_text[:150] + "..." if len(transcript_text) > 150 else transcript_text
            )
    else:
        conversation.title = "Empty Conversation"
        conversation.summary = "No speech detected"

    # Save the updated conversation
    await conversation.save()

    logger.info(
        f"✅ Transcript processing completed for {conversation_id} in {processing_time:.2f}s"
    )

    # Update job metadata with title and summary for UI display
    from rq import get_current_job

    current_job = get_current_job()
    if current_job:
        if not current_job.meta:
            current_job.meta = {}
        current_job.meta.update(
            {
                "conversation_id": conversation_id,
                "title": conversation.title,
                "summary": conversation.summary,
                "transcript_length": len(transcript_text),
                "word_count": len(words),
                "processing_time": processing_time,
            }
        )
        current_job.save_meta()

    return {
        "success": True,
        "conversation_id": conversation_id,
        "version_id": version_id,
        "audio_source": "mongodb_chunks",  # Audio reconstructed from MongoDB, no permanent file
        "transcript": transcript_text,
        "segments": [seg.model_dump() for seg in speaker_segments],
        "words": words,  # Needed by speaker recognition
        "provider": provider_name,
        "processing_time_seconds": processing_time,
        "trigger": trigger,
    }


@async_job(redis=True, beanie=True)
async def stream_speech_detection_job(
    session_id: str, user_id: str, client_id: str, *, redis_client=None
) -> Dict[str, Any]:
    """
    Listen for meaningful speech, optionally check for enrolled speakers, then start conversation.

    Simple flow:
        1. Listen for meaningful speech
        2. If speaker filter enabled → check for enrolled speakers
        3. If criteria met → start open_conversation_job and EXIT
        4. Conversation will restart new speech detection when complete

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with session info and conversation_job_id or no_speech_detected

    Note: user_email is fetched from the database when needed.
    """
    from advanced_omi_backend.services.audio_stream import TranscriptionResultsAggregator
    from advanced_omi_backend.utils.conversation_utils import analyze_speech
    from .conversation_jobs import open_conversation_job
    from rq import get_current_job

    logger.info(f"🔍 Starting speech detection for session {session_id[:12]}")

    # Setup
    aggregator = TranscriptionResultsAggregator(redis_client)
    current_job = get_current_job()
    session_key = f"audio:session:{session_id}"
    start_time = time.time()
    max_runtime = 86340  # 24 hours - 60 seconds (graceful exit before RQ timeout)

    # Get conversation count
    conversation_count_key = f"session:conversation_count:{session_id}"
    conversation_count_bytes = await redis_client.get(conversation_count_key)
    conversation_count = int(conversation_count_bytes) if conversation_count_bytes else 0

    # Check if speaker filtering is enabled
    speaker_filter_enabled = os.getenv("RECORD_ONLY_ENROLLED_SPEAKERS", "false").lower() == "true"
    logger.info(
        f"📊 Conversation #{conversation_count + 1}, Speaker filter: {'enabled' if speaker_filter_enabled else 'disabled'}"
    )

    # Update job metadata to show status
    if current_job:
        if not current_job.meta:
            current_job.meta = {}
        current_job.meta.update(
            {
                "status": "listening_for_speech",
                "session_id": session_id,
                "client_id": client_id,
                "session_level": True,  # Mark as session-level job
            }
        )
        current_job.save_meta()

    # Track when session closes for graceful shutdown
    session_closed_at = None
    final_check_grace_period = 15  # Wait up to 15 seconds for final transcription after session closes
    last_speech_analysis = None  # Track last analysis for detailed logging

    # Main loop: Listen for speech
    while True:
        # Check if job still exists in Redis (detect zombie state)
        from advanced_omi_backend.utils.job_utils import check_job_alive

        if not await check_job_alive(redis_client, current_job, session_id):
            break

        # Check if session has closed
        session_status = await redis_client.hget(session_key, "status")
        session_closed = session_status and session_status.decode() in ["complete", "closed"]

        if session_closed and session_closed_at is None:
            # Session just closed - start grace period for final transcription
            session_closed_at = time.time()
            logger.info(f"🛑 Session closed, waiting up to {final_check_grace_period}s for final transcription results...")

        # Exit if grace period expired without speech
        if session_closed_at and (time.time() - session_closed_at) > final_check_grace_period:
            logger.info(f"✅ Session ended without speech (grace period expired)")
            break

        if time.time() - start_time > max_runtime:
            logger.warning(f"⏱️ Max runtime reached, exiting")
            break

        # Get transcription results
        combined = await aggregator.get_combined_results(session_id)
        if not combined["text"]:
            # Health check: detect transcription errors early during grace period
            if session_closed_at:
                # Check for streaming consumer errors in session metadata
                error_status = await redis_client.hget(session_key, "transcription_error")
                if error_status:
                    error_msg = error_status.decode()
                    logger.warning(f"❌ Transcription error detected: {error_msg}")
                    logger.info(f"✅ Session ended without speech (transcription error)")
                    break

                # Check if we've been waiting too long with no results at all
                grace_elapsed = time.time() - session_closed_at
                if grace_elapsed > 5 and not combined.get("chunk_count", 0):
                    # 5+ seconds with no transcription activity at all - likely API key issue
                    logger.warning(f"⚠️ No transcription activity after {grace_elapsed:.1f}s - possible API key or connectivity issue")
                    logger.info(f"✅ Session ended without speech (no transcription activity)")
                    break

            await asyncio.sleep(2)
            continue

        # Step 1: Check for meaningful speech
        transcript_data = {"text": combined["text"], "words": combined.get("words", [])}

        logger.info(
            f"🔤 TRANSCRIPT [SPEECH_DETECT] session={session_id}, "
            f"words={len(combined.get('words', []))}, text=\"{combined['text']}\""
        )

        speech_analysis = analyze_speech(transcript_data)
        last_speech_analysis = speech_analysis  # Track for final logging

        logger.info(
            f"🔍 {speech_analysis.get('word_count', 0)} words, "
            f"{speech_analysis.get('duration', 0):.1f}s, "
            f"has_speech: {speech_analysis.get('has_speech', False)}"
        )

        if not speech_analysis.get("has_speech", False):
            logger.info(f"⏳ Waiting for more speech - {speech_analysis.get('reason', 'unknown reason')}")
            await asyncio.sleep(2)
            continue

        logger.info(f"💬 Meaningful speech detected!")

        # Add session event for speech detected
        from datetime import datetime

        await redis_client.hset(
            session_key, "last_event", f"speech_detected:{datetime.utcnow().isoformat()}"
        )
        await redis_client.hset(session_key, "speech_detected_at", datetime.utcnow().isoformat())

        # Step 2: If speaker filter enabled, check for enrolled speakers
        identified_speakers = []
        speaker_check_job = None  # Initialize for later reference
        if speaker_filter_enabled:
            logger.info(f"🎤 Enqueuing speaker check job...")

            # Add session event for speaker check starting
            await redis_client.hset(
                session_key, "last_event", f"speaker_check_starting:{datetime.utcnow().isoformat()}"
            )
            await redis_client.hset(session_key, "speaker_check_status", "checking")
            from .speaker_jobs import check_enrolled_speakers_job

            # Enqueue speaker check as a separate trackable job
            speaker_check_job = transcription_queue.enqueue(
                check_enrolled_speakers_job,
                session_id,
                user_id,
                client_id,
                job_timeout=300,  # 5 minutes for speaker recognition
                result_ttl=600,
                job_id=f"speaker-check_{session_id[:12]}_{conversation_count}",
                description=f"Speaker check for conversation #{conversation_count+1}",
                meta={"client_id": client_id},
            )

            # Poll for result (with timeout)
            max_wait = 30  # 30 seconds max
            poll_interval = 0.5
            waited = 0
            enrolled_present = False

            while waited < max_wait:
                try:
                    speaker_check_job.refresh()
                except Exception as e:
                    if isinstance(e, NoSuchJobError):
                        logger.warning(
                            f"⚠️ Speaker check job disappeared from Redis (likely completed quickly), assuming not enrolled"
                        )
                        break
                    else:
                        raise

                if speaker_check_job.is_finished:
                    result = speaker_check_job.result
                    enrolled_present = result.get("enrolled_present", False)
                    identified_speakers = result.get("identified_speakers", [])
                    logger.info(f"✅ Speaker check completed: enrolled={enrolled_present}")

                    # Update session event for speaker check complete
                    await redis_client.hset(
                        session_key,
                        "last_event",
                        f"speaker_check_complete:{datetime.utcnow().isoformat()}",
                    )
                    await redis_client.hset(
                        session_key,
                        "speaker_check_status",
                        "enrolled" if enrolled_present else "not_enrolled",
                    )
                    if identified_speakers:
                        await redis_client.hset(
                            session_key, "identified_speakers", ",".join(identified_speakers)
                        )
                    break
                elif speaker_check_job.is_failed:
                    logger.warning(f"⚠️ Speaker check job failed, assuming not enrolled")

                    # Update session event for speaker check failed
                    await redis_client.hset(
                        session_key,
                        "last_event",
                        f"speaker_check_failed:{datetime.utcnow().isoformat()}",
                    )
                    await redis_client.hset(session_key, "speaker_check_status", "failed")
                    break
                await asyncio.sleep(poll_interval)
                waited += poll_interval
            else:
                # Timeout - assume not enrolled
                logger.warning(
                    f"⏱️ Speaker check timed out after {max_wait}s, assuming not enrolled"
                )
                enrolled_present = False

                # Update session event for speaker check timeout
                await redis_client.hset(
                    session_key,
                    "last_event",
                    f"speaker_check_timeout:{datetime.utcnow().isoformat()}",
                )
                await redis_client.hset(session_key, "speaker_check_status", "timeout")

            # Log speaker check result but proceed with conversation regardless
            if enrolled_present:
                logger.info(
                    f"✅ Enrolled speaker(s) found: {', '.join(identified_speakers) if identified_speakers else 'Unknown'}"
                )
            else:
                logger.info(
                    f"ℹ️ No enrolled speakers found, but proceeding with conversation anyway"
                )

        # Step 3: Start conversation and EXIT
        speech_detected_at = time.time()
        open_job_key = f"open_conversation:session:{session_id}"

        # Enqueue conversation job with speech detection job ID
        from datetime import datetime

        speech_job_id = current_job.id if current_job else None

        open_job = transcription_queue.enqueue(
            open_conversation_job,
            session_id,
            user_id,
            client_id,
            speech_detected_at,
            speech_job_id,  # Pass speech detection job ID
            job_timeout=10800,  # 3 hours to match max_runtime in open_conversation_job
            result_ttl=JOB_RESULT_TTL,  # Use configured TTL (24 hours) instead of 10 minutes
            job_id=f"open-conv_{session_id[:12]}_{conversation_count}",
            description=f"Conversation #{conversation_count+1} for {session_id[:12]}",
            meta={"client_id": client_id},
        )

        # Track the job
        await redis_client.set(open_job_key, open_job.id, ex=10800)  # 3 hours to match job timeout

        # Store metadata in speech detection job
        if current_job:
            if not current_job.meta:
                current_job.meta = {}

            # Remove session_level flag now that conversation is starting
            current_job.meta.pop("session_level", None)

            current_job.meta.update(
                {
                    "conversation_job_id": open_job.id,
                    "speaker_check_job_id": speaker_check_job.id if speaker_check_job else None,
                    "detected_speakers": identified_speakers,
                    "speech_detected_at": datetime.fromtimestamp(speech_detected_at).isoformat(),
                    "session_id": session_id,
                    "client_id": client_id,  # For job grouping
                }
            )
            current_job.save_meta()

        logger.info(f"✅ Started conversation job {open_job.id}, exiting speech detection")

        return {
            "session_id": session_id,
            "user_id": user_id,
            "client_id": client_id,
            "conversation_job_id": open_job.id,
            "speech_detected_at": datetime.fromtimestamp(speech_detected_at).isoformat(),
            "runtime_seconds": time.time() - start_time,
        }

    # Session ended without speech
    reason = last_speech_analysis.get('reason', 'No transcription received') if last_speech_analysis else 'No transcription received'
    logger.warning(
        f"❌ Session ended without meaningful speech detected\n"
        f"   Reason: {reason}\n"
        f"   Runtime: {time.time() - start_time:.1f}s"
    )
    return {
        "session_id": session_id,
        "user_id": user_id,
        "client_id": client_id,
        "no_speech_detected": True,
        "reason": reason,
        "runtime_seconds": time.time() - start_time,
    }
