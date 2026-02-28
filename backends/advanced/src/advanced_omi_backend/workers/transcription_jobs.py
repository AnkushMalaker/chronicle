"""
Transcription-related RQ job functions.

This module contains all jobs related to speech-to-text transcription processing.
"""

import asyncio
import io
import json
import logging
import os
import time
import wave
from datetime import datetime
from typing import Any, Dict

from beanie.operators import In
from rq import get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Job

from advanced_omi_backend.config import get_transcription_job_timeout
from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    start_post_conversation_jobs,
    transcription_queue,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.services.audio_stream import TranscriptionResultsAggregator
from advanced_omi_backend.services.plugin_service import dispatch_plugin_event
from advanced_omi_backend.services.transcription import (
    get_transcription_provider,
    is_transcription_available,
)
from advanced_omi_backend.utils.audio_chunk_utils import (
    convert_audio_to_chunks,
    reconstruct_wav_from_conversation,
)
from advanced_omi_backend.utils.conversation_utils import (
    analyze_speech,
    mark_conversation_deleted,
)
from advanced_omi_backend.utils.job_utils import update_job_meta

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
        from advanced_omi_backend.speaker_recognition_client import (
            SpeakerRecognitionClient,
        )

        speaker_client = SpeakerRecognitionClient()
        if not speaker_client.enabled:
            logger.info(
                f"🎤 Speaker recognition disabled, using original speaker labels"
            )
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
            identified_speaker = get_speaker_at_time(
                seg_mid, speaker_identified_segments
            )

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
    logger.info(
        f"🔄 RQ: Starting transcript processing for conversation {conversation_id} (trigger: {trigger})"
    )

    start_time = time.time()

    # Get the conversation
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        raise ValueError(f"Conversation {conversation_id} not found")

    # Extract user_id and client_id for plugin context
    user_id = str(conversation.user_id) if conversation.user_id else None
    client_id = conversation.client_id if hasattr(conversation, "client_id") else None

    # Get the transcription provider
    provider = get_transcription_provider(mode="batch")
    if not provider:
        raise ValueError("No transcription provider available")

    provider_name = provider.name
    logger.info(f"Using transcription provider: {provider_name}")

    # Reconstruct audio from MongoDB chunks
    logger.info(
        f"📦 Reconstructing audio from MongoDB chunks for conversation {conversation_id}"
    )

    try:
        # Reconstruct WAV from MongoDB chunks (already in memory as bytes)
        wav_data = await reconstruct_wav_from_conversation(conversation_id)

        logger.info(
            f"📦 Reconstructed audio from MongoDB chunks: "
            f"{len(wav_data) / 1024 / 1024:.2f} MB"
        )
    except ValueError as e:
        # No chunks found for conversation
        raise FileNotFoundError(
            f"No audio chunks found for conversation {conversation_id}: {e}"
        )
    except Exception as e:
        logger.error(f"Failed to reconstruct audio from MongoDB: {e}", exc_info=True)
        raise RuntimeError(f"Audio reconstruction failed: {e}")

    # Build ASR context (static hot words + per-user cached jargon)
    try:
        from advanced_omi_backend.services.transcription.context import get_asr_context

        context_info = await get_asr_context(user_id=user_id)
    except Exception as e:
        logger.warning(f"Failed to build ASR context: {e}")
        context_info = None

    # Read actual sample rate from WAV header
    try:
        with wave.open(io.BytesIO(wav_data), "rb") as wf:
            actual_sample_rate = wf.getframerate()
    except Exception:
        actual_sample_rate = 16000

    try:
        # Progress callback: writes batch progress to RQ job.meta so the
        # queue API and UI can show "Transcribing segment X of Y".
        def _on_batch_progress(event: dict) -> None:
            job = get_current_job()
            if job:
                current = event.get("current", 0)
                total = event.get("total", 0)
                job.meta["batch_progress"] = {
                    "current": current,
                    "total": total,
                    "percent": int(current / total * 100) if total else 0,
                    "message": f"Transcribing segment {current} of {total}",
                }
                job.save_meta()

        # Transcribe the audio directly from memory (no disk I/O needed)
        transcribe_kwargs: Dict[str, Any] = {
            "audio_data": wav_data,
            "sample_rate": actual_sample_rate,
            "diarize": True,
            "progress_callback": _on_batch_progress,
        }
        if context_info:
            transcribe_kwargs["context_info"] = context_info
        transcription_result = await provider.transcribe(**transcribe_kwargs)
    except ConnectionError as e:
        logger.exception(f"Transcription service unreachable for {conversation_id}")
        raise RuntimeError(str(e))
    except RuntimeError:
        raise
    except Exception as e:
        logger.exception(f"Transcription failed for conversation {conversation_id}")
        raise RuntimeError(f"Transcription failed ({type(e).__name__}): {e}")

    # Extract results
    transcript_text = transcription_result.get("text", "")
    segments = transcription_result.get("segments", [])
    words = transcription_result.get("words", [])

    logger.info(
        f"📊 Transcription complete: {len(transcript_text)} chars, {len(segments)} segments, {len(words)} words"
    )

    # Trigger transcript-level plugins BEFORE speech validation
    # This ensures wake-word commands execute even if conversation gets deleted
    if transcript_text:
        try:
            await dispatch_plugin_event(
                event=PluginEvent.TRANSCRIPT_BATCH,
                user_id=user_id,
                data={
                    "transcript": transcript_text,
                    "segment_id": f"{conversation_id}_batch",
                    "conversation_id": conversation_id,
                    "segments": segments,
                    "word_count": len(words),
                },
                metadata={"client_id": client_id},
                description=f"conversation={conversation_id[:12]}, words={len(words)}",
            )
        except Exception as e:
            logger.exception(
                f"⚠️ Error triggering transcript plugins in batch mode: {e}"
            )

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
        # Note: get_current_job and Job are already imported at module level
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
                            logger.debug(
                                f"Job {job_id} hash not found (likely already completed or expired)"
                            )
                        else:
                            logger.debug(
                                f"Job {job_id} not found or already completed: {e}"
                            )

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

    # Get provider capabilities for downstream processing decisions
    # Capabilities determine whether pyannote diarization is needed or can be skipped
    provider_capabilities = {}
    if hasattr(provider, "get_capabilities_dict"):
        provider_capabilities = provider.get_capabilities_dict()
        logger.info(f"📊 Provider capabilities: {list(provider_capabilities.keys())}")

    # Check if provider has diarization capability (e.g., VibeVoice, Deepgram batch)
    provider_has_diarization = provider_capabilities.get("diarization", False)

    # Check speaker recognition configuration
    from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient

    speaker_client = SpeakerRecognitionClient()
    speaker_recognition_enabled = speaker_client.enabled

    # Determine how to handle segments based on capabilities and configuration
    speaker_segments = []
    diarization_source = None
    segments_created_by = "speaker_service"  # Default

    if segments:
        # Provider returned segments - use them
        from advanced_omi_backend.utils.segment_utils import classify_segment_text

        speaker_segments = []
        for seg in segments:
            raw_speaker = seg.get("speaker")
            if raw_speaker is None:
                speaker = "Speaker 0"
            elif isinstance(raw_speaker, int):
                speaker = f"Speaker {raw_speaker}"
            else:
                speaker = str(raw_speaker)

            # Classify segment as speech/event based on content
            text = seg.get("text", "")
            classification = classify_segment_text(text)
            seg_type = "speech"
            if classification == "event":
                seg_type = "event"
                speaker = ""  # No speaker for non-speech events

            speaker_segments.append(
                Conversation.SpeakerSegment(
                    speaker=speaker,
                    start=seg.get("start", 0.0),
                    end=seg.get("end", 0.0),
                    text=text,
                    segment_type=seg_type,
                )
            )

        if provider_has_diarization:
            # Provider did diarization (e.g., VibeVoice, Deepgram)
            diarization_source = "provider"
            segments_created_by = "provider_diarization"
            logger.info(
                f"✅ Using {len(speaker_segments)} diarized segments from provider "
                f"(provider has diarization capability)"
            )
        else:
            # Provider gave segments but without speaker diarization
            segments_created_by = "provider"
            logger.info(
                f"✅ Using {len(speaker_segments)} segments from provider "
                f"(no diarization, speaker service will add labels)"
            )
    elif not speaker_recognition_enabled and words:
        # No segments from provider AND speaker recognition is disabled
        # Create a fallback segment with the full transcript
        # This ensures memory extraction always has segments to work with
        start_time_audio = words[0].get("start", 0.0) if words else 0.0
        end_time_audio = words[-1].get("end", 0.0) if words else 0.0

        speaker_segments = [
            Conversation.SpeakerSegment(
                speaker="Speaker 0",
                start=start_time_audio,
                end=end_time_audio,
                text=transcript_text,
            )
        ]
        segments_created_by = "fallback"
        logger.info(
            f"📊 Created fallback segment (speaker recognition disabled, no provider segments)"
        )
    else:
        # No segments from provider, but speaker recognition will create them
        logger.info(
            f"📊 Transcription complete: {len(words)} words "
            f"(segments will be created by speaker service)"
        )

    # Add new transcript version
    provider_normalized = provider_name.lower() if provider_name else "unknown"

    # Convert words to Word objects
    word_objects = [
        Conversation.Word(
            word=w.get("word", ""),
            start=w.get("start", 0.0),
            end=w.get("end", 0.0),
            confidence=w.get("confidence"),
        )
        for w in words
    ]

    # Prepare metadata with provider capabilities for downstream jobs
    metadata = {
        "trigger": trigger,
        "audio_file_size": len(wav_data),
        "word_count": len(words),
        "segments_created_by": segments_created_by,
        "provider_capabilities": provider_capabilities,  # For speaker_jobs.py conditional logic
    }

    # Create the transcript version
    new_version = conversation.add_transcript_version(
        version_id=version_id,
        transcript=transcript_text,
        words=word_objects,
        segments=speaker_segments,
        provider=provider_normalized,
        model=provider.name,
        processing_time_seconds=processing_time,
        metadata=metadata,
        set_as_active=True,
    )

    # Set diarization source if provider did diarization
    if diarization_source:
        new_version.diarization_source = diarization_source

    # Title/summary left as placeholder — generate_title_summary_job handles this
    # after speaker recognition populates segments with identified names.
    if not transcript_text or len(transcript_text.strip()) == 0:
        conversation.title = "Empty Conversation"
        conversation.summary = "No speech detected"

    # Save the updated conversation
    await conversation.save()

    logger.info(
        f"✅ Transcript processing completed for {conversation_id} in {processing_time:.2f}s"
    )

    # Update job metadata with title and summary for UI display
    update_job_meta(
        conversation_id=conversation_id,
        title=conversation.title,
        summary=conversation.summary,
        transcript_length=len(transcript_text),
        word_count=len(words),
        processing_time=processing_time,
    )

    return {
        "success": True,
        "conversation_id": conversation_id,
        "version_id": version_id,
        "audio_source": "mongodb_chunks",  # Audio reconstructed from MongoDB, no permanent file
        "transcript": transcript_text,
        "segments": [seg.model_dump() for seg in speaker_segments],
        "words": words,  # Needed by speaker recognition
        "provider": provider_name,
        "provider_capabilities": provider_capabilities,  # For downstream jobs
        "diarization_source": diarization_source,  # "provider" or None
        "processing_time_seconds": processing_time,
        "trigger": trigger,
    }


async def create_audio_only_conversation(
    session_id: str, user_id: str, client_id: str
) -> "Conversation":
    """
    Create or reuse conversation for batch transcription fallback.

    Handles two scenarios:
    1. always_persist=True - Reuses existing placeholder conversation
    2. always_persist=False - Creates new conversation from audio chunks
    """
    # CASE 1: Check if always_persist placeholder conversation exists
    # The audio_streaming_persistence_job may have created it already
    placeholder_conversation = await Conversation.find_one(
        Conversation.client_id == session_id,
        Conversation.always_persist == True,
        In(
            Conversation.processing_status,
            ["pending_transcription", "transcription_failed"],
        ),
    )

    if placeholder_conversation:
        logger.info(
            f"✅ Found always_persist placeholder conversation {placeholder_conversation.conversation_id[:12]} "
            f"for session {session_id[:12]}, reusing for batch transcription"
        )
        # Update status to show batch transcription is starting
        placeholder_conversation.processing_status = "batch_transcription"
        placeholder_conversation.title = "Audio Recording (Batch Transcription...)"
        placeholder_conversation.summary = (
            "Processing audio with offline transcription..."
        )
        await placeholder_conversation.save()

        # Audio chunks are already linked to this conversation_id
        # (stored by audio_streaming_persistence_job)
        return placeholder_conversation

    # CASE 2: No placeholder exists - create new conversation using session_id
    # This happens when always_persist=False or audio_persistence_job didn't run
    # We reuse session_id as conversation_id to avoid unnecessary UUID generation
    logger.info(
        f"✅ No placeholder found, creating new conversation for session {session_id[:12]} "
        f"using session_id as conversation_id"
    )

    conversation = Conversation(
        conversation_id=session_id,
        user_id=user_id,
        client_id=client_id,
        title="Audio Recording (Batch Transcription...)",
        summary="Processing audio with offline transcription...",
        processing_status="batch_transcription",
        always_persist=False,  # Mark as False since this is fallback
        created_at=datetime.utcnow(),
    )
    await conversation.insert()

    logger.info(
        f"✅ Created batch transcription conversation {session_id[:12]} for fallback"
    )
    return conversation


@async_job(redis=True, beanie=True)
async def transcription_fallback_check_job(
    session_id: str,
    user_id: str,
    client_id: str,
    timeout_seconds: int = None,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    Check if streaming transcription succeeded, fallback to batch if needed.

    This job acts as a gate for post-conversation jobs:
    - If streaming transcript exists → Pass through immediately
    - If no transcript → Trigger batch transcription, wait for completion, enqueue post-jobs

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        timeout_seconds: Max wait time for batch transcription (default 15 minutes)
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with status (pass_through or batch_fallback_completed) and conversation details
    """
    if timeout_seconds is None:
        timeout_seconds = get_transcription_job_timeout()

    logger.info(f"🔍 Checking transcription status for session {session_id[:12]}")

    # Find conversation by session_id (client_id for streaming sessions)
    conversation = await Conversation.find_one(Conversation.client_id == session_id)

    # Check if transcript exists (streaming succeeded)
    if conversation and conversation.active_transcript and conversation.transcript:
        logger.info(
            f"✅ Streaming transcript exists for session {session_id[:12]}, "
            f"passing through (conversation {conversation.conversation_id[:12]})"
        )
        return {
            "status": "pass_through",
            "transcript_source": "streaming",
            "conversation_id": conversation.conversation_id,
        }

    # No transcript → Trigger batch fallback
    logger.warning(
        f"⚠️ No streaming transcript found for session {session_id[:12]}, "
        f"attempting batch transcription fallback"
    )

    # Check if batch provider available
    if not is_transcription_available(mode="batch"):
        raise ValueError(
            "No batch transcription provider available for fallback. "
            "Configure a batch STT provider (e.g., Parakeet) or fix streaming provider."
        )

    # If no conversation exists, check if we have audio chunks to transcribe
    if not conversation:
        chunks_count = await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == session_id
        ).count()

        if chunks_count == 0:
            # No MongoDB chunks - check if Redis stream has unprocessed audio
            logger.info(
                f"📦 No MongoDB chunks found for session {session_id[:12]}, "
                f"checking Redis stream for unprocessed audio..."
            )

            stream_name = f"audio:stream:{client_id}"

            # Check if stream exists and has messages
            try:
                stream_length = await redis_client.xlen(stream_name)

                if stream_length == 0:
                    logger.info(
                        f"ℹ️ No audio found in Redis stream {stream_name}. "
                        f"Session ended without audio capture. Skipping fallback."
                    )
                    return {
                        "status": "skipped",
                        "reason": "no_audio",
                        "message": "No audio was captured for this session",
                        "session_id": session_id,
                    }

                logger.info(
                    f"📡 Found {stream_length} messages in Redis stream {stream_name}, "
                    f"extracting audio for batch transcription..."
                )

                # Read all audio messages from stream
                messages = await redis_client.xrange(stream_name)

                # Collect PCM audio chunks in order
                audio_chunks = {}  # {chunk_num: audio_data}

                for msg_id, fields in messages:
                    # Check if this message belongs to our session
                    msg_session_id = fields.get(b"session_id", b"").decode()
                    if msg_session_id != session_id:
                        continue

                    # Get chunk ID
                    msg_chunk_id = fields.get(b"chunk_id", b"").decode()
                    if not msg_chunk_id or msg_chunk_id == "END":
                        continue

                    try:
                        chunk_num = int(msg_chunk_id)
                    except ValueError:
                        continue

                    # Get PCM audio data
                    audio_data = fields.get(b"audio_data", b"")
                    if audio_data:
                        audio_chunks[chunk_num] = audio_data

                if not audio_chunks:
                    logger.warning(
                        f"⚠️ Redis stream has {stream_length} messages but no audio chunks "
                        f"matched session {session_id[:12]}. Skipping fallback."
                    )
                    return {
                        "status": "skipped",
                        "reason": "no_matching_audio",
                        "message": "No audio matched this session in Redis stream",
                        "session_id": session_id,
                    }

                # Combine audio chunks in order
                sorted_chunks = sorted(audio_chunks.items())
                combined_audio = b"".join(data for _, data in sorted_chunks)

                # Read audio format from Redis session metadata
                sample_rate, channels, sample_width = 16000, 1, 2
                session_key = f"audio:session:{session_id}"
                try:
                    audio_format_raw = await redis_client.hget(
                        session_key, "audio_format"
                    )
                    if audio_format_raw:
                        audio_format = json.loads(audio_format_raw)
                        sample_rate = int(audio_format.get("rate", 16000))
                        channels = int(audio_format.get("channels", 1))
                        sample_width = int(audio_format.get("width", 2))
                except Exception as e:
                    logger.warning(
                        f"Failed to read audio_format from Redis for {session_id}: {e}"
                    )

                bytes_per_second = sample_rate * channels * sample_width
                logger.info(
                    f"✅ Extracted {len(sorted_chunks)} audio chunks from Redis stream "
                    f"({len(combined_audio)} bytes, ~{len(combined_audio)/bytes_per_second:.1f}s)"
                )

                # Create conversation placeholder
                conversation = await create_audio_only_conversation(
                    session_id, user_id, client_id
                )

                # Save audio to MongoDB chunks for batch transcription
                num_chunks = await convert_audio_to_chunks(
                    conversation_id=conversation.conversation_id,
                    audio_data=combined_audio,
                    sample_rate=sample_rate,
                    channels=channels,
                    sample_width=sample_width,
                )

                logger.info(
                    f"💾 Persisted {num_chunks} MongoDB chunks for batch transcription "
                    f"(conversation {conversation.conversation_id[:12]})"
                )

            except Exception as e:
                logger.error(
                    f"❌ Failed to extract audio from Redis stream: {e}", exc_info=True
                )
                raise
        else:
            logger.info(
                f"✅ Found {chunks_count} MongoDB chunks for session {session_id[:12]}, "
                f"creating conversation placeholder"
            )

            # Create conversation placeholder for batch transcription
            conversation = await create_audio_only_conversation(
                session_id, user_id, client_id
            )

    # Enqueue batch transcription job
    version_id = f"batch_fallback_{session_id[:12]}"
    batch_job = transcription_queue.enqueue(
        transcribe_full_audio_job,
        conversation.conversation_id,
        version_id,
        "batch_fallback",
        job_timeout=get_transcription_job_timeout(),
        job_id=f"transcribe_{conversation.conversation_id[:12]}",
        description=f"Batch transcription fallback for {session_id[:8]}",
        meta={"session_id": session_id, "client_id": client_id},
    )

    logger.info(f"🔄 Enqueued batch transcription fallback job {batch_job.id}")

    # Wait for batch transcription to complete
    max_wait = timeout_seconds
    waited = 0
    while waited < max_wait:
        batch_job.refresh()
        # Check is_failed BEFORE is_finished - a failed job is also "finished"
        if batch_job.is_failed:
            raise RuntimeError(f"Batch transcription failed: {batch_job.exc_info}")
        if batch_job.is_finished:
            logger.info("✅ Batch transcription completed successfully")
            break
        await asyncio.sleep(2)
        waited += 2

    if waited >= max_wait:
        raise TimeoutError(f"Batch transcription timed out after {max_wait}s")

    # Enqueue post-conversation jobs (same as file upload flow)
    post_jobs = start_post_conversation_jobs(
        conversation_id=conversation.conversation_id,
        user_id=user_id,
        transcript_version_id=version_id,
        depends_on_job=None,  # Batch already completed (we waited for it)
        client_id=client_id,
        end_reason="batch_fallback",
    )

    logger.info(
        f"📋 Enqueued {len(post_jobs)} post-conversation jobs for "
        f"batch fallback conversation {conversation.conversation_id[:12]}"
    )

    return {
        "status": "batch_fallback_completed",
        "transcript_source": "batch",
        "conversation_id": conversation.conversation_id,
        "batch_job_id": batch_job.id,
        "post_job_ids": post_jobs,
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
    from .conversation_jobs import open_conversation_job

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
    conversation_count = (
        int(conversation_count_bytes) if conversation_count_bytes else 0
    )

    # Check if speaker filtering is enabled
    speaker_filter_enabled = (
        os.getenv("RECORD_ONLY_ENROLLED_SPEAKERS", "false").lower() == "true"
    )
    logger.info(
        f"📊 Conversation #{conversation_count + 1}, Speaker filter: {'enabled' if speaker_filter_enabled else 'disabled'}"
    )

    # Update job metadata to show status
    update_job_meta(
        status="listening_for_speech",
        session_id=session_id,
        client_id=client_id,
        session_level=True,  # Mark as session-level job
    )

    # Track when session closes for graceful shutdown
    session_closed_at = None
    final_check_grace_period = (
        15  # Wait up to 15 seconds for final transcription after session closes
    )
    last_speech_analysis = None  # Track last analysis for detailed logging

    # Main loop: Listen for speech
    while True:
        # Check if job still exists in Redis (detect zombie state)
        from advanced_omi_backend.utils.job_utils import check_job_alive

        if not await check_job_alive(redis_client, current_job, session_id):
            break

        # Check if session has closed
        session_status = await redis_client.hget(session_key, "status")
        session_closed = session_status and session_status.decode() in [
            "finalizing",
            "finished",
        ]

        if session_closed and session_closed_at is None:
            # Session just closed - start grace period for final transcription
            session_closed_at = time.time()
            logger.info(
                f"🛑 Session closed, waiting up to {final_check_grace_period}s for final transcription results..."
            )

        # Exit if grace period expired without speech
        if (
            session_closed_at
            and (time.time() - session_closed_at) > final_check_grace_period
        ):
            logger.info(f"✅ Session ended without speech (grace period expired)")
            break

        # Consume any stale conversation close request (defensive — shouldn't normally
        # appear since services.py gates on conversation:current, but handles race conditions)
        close_reason = await redis_client.hget(
            session_key, "conversation_close_requested"
        )
        if close_reason:
            await redis_client.hdel(session_key, "conversation_close_requested")
            close_reason_str = (
                close_reason.decode()
                if isinstance(close_reason, bytes)
                else close_reason
            )
            logger.info(
                f"🔒 Conversation close requested ({close_reason_str}) during speech detection — "
                f"no open conversation to close, flag consumed"
            )

        if time.time() - start_time > max_runtime:
            logger.warning(f"⏱️ Max runtime reached, exiting")
            break

        # Get transcription results
        combined = await aggregator.get_combined_results(session_id)
        if not combined["text"]:
            # Health check: detect transcription errors early during grace period
            if session_closed_at:
                # Check for streaming consumer errors in session metadata
                error_status = await redis_client.hget(
                    session_key, "transcription_error"
                )
                if error_status:
                    error_msg = error_status.decode()
                    logger.error(f"❌ Transcription service error: {error_msg}")
                    logger.error(
                        f"❌ Session failed - transcription service unavailable"
                    )
                    break

                # Check if we've been waiting too long with no results at all
                grace_elapsed = time.time() - session_closed_at
                if grace_elapsed > 5 and not combined.get("chunk_count", 0):
                    # 5+ seconds with no transcription activity at all - likely API key issue
                    logger.error(
                        f"❌ No transcription activity after {grace_elapsed:.1f}s - possible API key or connectivity issue"
                    )
                    logger.error(
                        f"❌ Session failed - check transcription service configuration"
                    )
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
            logger.info(
                f"⏳ Waiting for more speech - {speech_analysis.get('reason', 'unknown reason')}"
            )
            await asyncio.sleep(2)
            continue

        logger.info(f"💬 Meaningful speech detected!")

        # Add session event for speech detected
        from datetime import datetime

        await redis_client.hset(
            session_key,
            "last_event",
            f"speech_detected:{datetime.utcnow().isoformat()}",
        )
        await redis_client.hset(
            session_key, "speech_detected_at", datetime.utcnow().isoformat()
        )

        # Step 2: If speaker filter enabled, check for enrolled speakers
        identified_speakers = []
        speaker_check_job = None  # Initialize for later reference
        if speaker_filter_enabled:
            logger.info(f"🎤 Enqueuing speaker check job...")

            # Add session event for speaker check starting
            await redis_client.hset(
                session_key,
                "last_event",
                f"speaker_check_starting:{datetime.utcnow().isoformat()}",
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
                job_id=f"speaker-check_{session_id}_{conversation_count}",
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
                    logger.info(
                        f"✅ Speaker check completed: enrolled={enrolled_present}"
                    )

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
                            session_key,
                            "identified_speakers",
                            ",".join(identified_speakers),
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
                    await redis_client.hset(
                        session_key, "speaker_check_status", "failed"
                    )
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
            job_id=f"open-conv_{session_id}_{conversation_count}",
            description=f"Conversation #{conversation_count+1} for {session_id}",
            meta={"client_id": client_id},
        )

        # Track the job
        await redis_client.set(
            open_job_key, open_job.id, ex=10800
        )  # 3 hours to match job timeout

        # Store metadata in speech detection job
        if current_job:
            if not current_job.meta:
                current_job.meta = {}

            # Remove session_level flag now that conversation is starting
            current_job.meta.pop("session_level", None)

            current_job.meta.update(
                {
                    "conversation_job_id": open_job.id,
                    "speaker_check_job_id": (
                        speaker_check_job.id if speaker_check_job else None
                    ),
                    "detected_speakers": identified_speakers,
                    "speech_detected_at": datetime.fromtimestamp(
                        speech_detected_at
                    ).isoformat(),
                    "session_id": session_id,
                    "client_id": client_id,  # For job grouping
                }
            )
            current_job.save_meta()

        logger.info(
            f"✅ Started conversation job {open_job.id}, exiting speech detection"
        )

        return {
            "session_id": session_id,
            "user_id": user_id,
            "client_id": client_id,
            "conversation_job_id": open_job.id,
            "speech_detected_at": datetime.fromtimestamp(
                speech_detected_at
            ).isoformat(),
            "runtime_seconds": time.time() - start_time,
        }

    # Session ended without speech
    reason = (
        last_speech_analysis.get("reason", "No transcription received")
        if last_speech_analysis
        else "No transcription received"
    )

    # Distinguish between transcription failures (error) vs legitimate no speech (info)
    if reason == "No transcription received":
        logger.error(
            f"❌ Session failed - transcription service did not respond\n"
            f"   Reason: {reason}\n"
            f"   Runtime: {time.time() - start_time:.1f}s"
        )
    else:
        logger.info(
            f"✅ Session ended without meaningful speech\n"
            f"   Reason: {reason}\n"
            f"   Runtime: {time.time() - start_time:.1f}s"
        )

    # Check if this is an always_persist conversation that needs to be marked as failed
    # NOTE: We check MongoDB directly because the conversation:current Redis key might have been
    # deleted by the audio persistence job cleanup (which runs in parallel).
    logger.info(
        f"🔍 Checking MongoDB for always_persist conversation with client_id: {client_id}"
    )

    # Find conversation by client_id that matches this session
    # session_id == client_id for streaming sessions (set in _initialize_streaming_session)
    conversation = await Conversation.find_one(
        Conversation.client_id == session_id,
        Conversation.always_persist == True,
        Conversation.processing_status == "pending_transcription",
    )

    if conversation:
        logger.info(
            f"🔴 Found always_persist placeholder conversation {conversation.conversation_id} for failed session {session_id[:12]}"
        )

        # Update conversation with failure status
        conversation.processing_status = "transcription_failed"
        conversation.title = "Audio Recording (Transcription Failed)"
        conversation.summary = f"Transcription failed: {reason}"

        await conversation.save()

        logger.warning(
            f"🔴 Marked conversation {conversation.conversation_id} as transcription_failed"
        )
    else:
        logger.info(
            f"ℹ️ No always_persist placeholder conversation found for session {session_id[:12]}"
        )

    # Enqueue fallback check job for failed streaming sessions
    # This will attempt batch transcription as a fallback
    config_timeout = get_transcription_job_timeout()
    fallback_job = transcription_queue.enqueue(
        transcription_fallback_check_job,
        session_id,
        user_id,
        client_id,
        timeout_seconds=config_timeout,
        job_timeout=config_timeout + 300,  # Extra 5 min overhead for fallback check
        job_id=f"fallback_check_{session_id}",
        description=f"Transcription fallback check for {session_id[:8]} (no speech)",
        meta={"session_id": session_id, "client_id": client_id, "no_speech": True},
    )

    logger.info(
        f"📋 Enqueued transcription fallback check job {fallback_job.id} "
        f"for failed session {session_id[:12]} (no speech detected)"
    )

    # The fallback job will:
    # 1. Check for always_persist placeholder conversation
    # 2. If found, trigger batch transcription using stored audio chunks
    # 3. Wait for batch completion and enqueue post-conversation jobs
    # 4. If no placeholder or no audio chunks, fail gracefully with clear error

    return {
        "session_id": session_id,
        "user_id": user_id,
        "client_id": client_id,
        "no_speech_detected": True,
        "fallback_job_id": fallback_job.id,
        "reason": reason,
        "runtime_seconds": time.time() - start_time,
    }
