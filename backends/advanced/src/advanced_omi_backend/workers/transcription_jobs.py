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
import traceback
import wave
from datetime import datetime, timezone
from typing import Any, Dict

from beanie.operators import In
from rq import get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Dependency, Job

from advanced_omi_backend.config import (
    get_live_segmentation,
    get_streaming_fallback_timeout,
    require_speech_for_transcription,
)
from advanced_omi_backend.constants import TITLE_NOT_GENERATED
from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    enqueue_speech_detection,
    redis_conn,
    start_post_conversation_jobs,
    transcription_queue,
)
from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.models.timeline import AudioEvidenceSpan
from advanced_omi_backend.observability.otel_setup import (
    set_otel_session,
    set_span_attrs,
    set_trace_io,
    traced_job,
)
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.services.audio_stream import TranscriptionResultsAggregator
from advanced_omi_backend.services.audio_stream.conversation_lifecycle import (
    ensure_active_session_placeholder,
)
from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
    SpeakerCheckStatus,
)
from advanced_omi_backend.services.forced_alignment import align_audio_words
from advanced_omi_backend.services.observability import record_event_sync
from advanced_omi_backend.services.plugin_service import dispatch_plugin_event
from advanced_omi_backend.services.sse_publisher import publish_sse_event_throttled
from advanced_omi_backend.services.transcript_integrity import (
    TranscriptTimingError,
    load_transcript_audio_ranges,
    validate_and_normalize_transcript_timing,
)
from advanced_omi_backend.services.transcription import (
    get_transcription_provider,
    is_transcription_available,
)
from advanced_omi_backend.services.transcription.context import (
    gather_transcription_context,
    get_asr_context,
)
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
from advanced_omi_backend.utils.audio_chunk_utils import (
    reconstruct_audio_segment,
    reconstruct_wav_from_conversation,
)
from advanced_omi_backend.utils.conversation_utils import (
    analyze_speech,
    mark_conversation_deleted,
)
from advanced_omi_backend.utils.job_utils import check_job_alive, update_job_meta
from advanced_omi_backend.utils.segment_utils import classify_segment_text
from advanced_omi_backend.utils.silence_condense import (
    condense_silence,
    remap_condensed_result,
)
from advanced_omi_backend.utils.vad_analysis import detect_speech_pcm

from .conversation_jobs import open_conversation_job
from .speaker_jobs import check_enrolled_speakers_job

logger = logging.getLogger(__name__)


async def _settle_audio_evidence_span(
    conversation: Conversation, word_count: int, state: str
) -> None:
    if conversation.external_source_type != "screenpipe":
        return
    span = await AudioEvidenceSpan.find_one(
        AudioEvidenceSpan.conversation_id == conversation.conversation_id
    )
    if span is None:
        return
    span.word_count = word_count
    span.state = state
    await span.save()


async def apply_speaker_recognition(
    audio_path: str,
    transcript_text: str,
    words: list,
    segments: list,
    user_id: str,
    conversation_id: str | None = None,
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
        logger.debug(traceback.format_exc())
        return segments


BATCH_CHUNK_SECONDS = 3600  # Never send more than 1h to ASR at once

# No-activity watchdog: only treat "zero transcription results" as a provider failure
# when audio is actually flowing in. A connected-but-quiet device (common right after a
# conversation ends and speech detection re-arms) produces no audio chunks, so zero
# results is expected, not a fault. If the last audio chunk arrived more than this many
# seconds ago the inflow is considered idle and the watchdog stays its hand.
AUDIO_INFLOW_IDLE_SECONDS = 30


def _needs_forced_alignment(words: list[dict]) -> bool:
    """Return whether provider 'words' are really timestamped multi-word phrases."""
    return not words or any(
        len(str(word.get("word", "")).split()) > 1 for word in words
    )


async def _align_result_words(result: dict, wav_data: bytes) -> dict:
    """Replace absent/phrase-level word timing with acoustic forced alignment."""
    words = result.get("words", [])
    if not _needs_forced_alignment(words):
        return result

    # Whisper long-form output may expose its timestamped decoding windows through
    # the words field when the fine-tuned model has no alignment-head metadata.
    # Those windows are much better alignment boundaries than one full-file segment.
    phrase_windows = [
        {
            "text": word.get("word", ""),
            "start": word.get("start", 0.0),
            "end": word.get("end", word.get("start", 0.0)),
        }
        for word in words
        if str(word.get("word", "")).strip()
        and word.get("end", word.get("start", 0.0)) > word.get("start", 0.0)
    ]
    alignment_segments = phrase_windows or result.get("segments", [])
    aligned_words = await align_audio_words(wav_data, alignment_segments)
    if aligned_words:
        result["words"] = aligned_words
    return result


def _build_wav(
    pcm_data: bytes, sample_rate: int, channels: int, sample_width: int
) -> bytes:
    """Build a WAV file from raw PCM data."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


async def transcribe_audio_range(
    conversation_id: str,
    start_time: float = 0.0,
    end_time: float | None = None,
    diarize: bool = True,
    context_info: str | None = None,
    progress_callback=None,
) -> dict:
    """
    Reconstruct audio for a time range and transcribe it.

    Pure reconstruction + transcription — no DB writes, no plugins, no speech validation.
    If the audio exceeds BATCH_CHUNK_SECONDS (1h), it is split internally and merged.

    Args:
        conversation_id: Conversation ID
        start_time: Start time in seconds (default 0.0)
        end_time: End time in seconds (None = full audio)
        diarize: Whether to request diarization
        context_info: ASR context hints
        progress_callback: Optional callback for batch progress

    Returns:
        Dict with text, segments, words, provider_name, provider_capabilities, wav_size, sample_rate
    """
    provider = get_transcription_provider(mode="batch")
    if not provider:
        raise ValueError("No batch transcription provider available")

    # Reconstruct audio
    if start_time == 0.0 and end_time is None:
        wav_data = await reconstruct_wav_from_conversation(conversation_id)
    else:
        if end_time is None:
            # Get total duration from conversation
            conversation = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            if not conversation:
                raise ValueError(f"Conversation {conversation_id} not found")
            end_time = conversation.audio_total_duration or 0.0
        wav_data = await reconstruct_audio_segment(
            conversation_id, start_time, end_time
        )

    logger.info(
        f"📦 Reconstructed audio [{start_time:.1f}s - {end_time or 'end'}]: "
        f"{len(wav_data) / 1024 / 1024:.2f} MB"
    )

    # Read WAV header to get audio properties
    try:
        with wave.open(io.BytesIO(wav_data), "rb") as wf:
            actual_sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            n_frames = wf.getnframes()
            pcm_data = wf.readframes(n_frames)
    except Exception:
        actual_sample_rate = 16000
        channels = 1
        sample_width = 2
        # Strip WAV header (44 bytes) as fallback
        pcm_data = wav_data[44:] if len(wav_data) > 44 else wav_data

    duration = (
        len(pcm_data) / (actual_sample_rate * sample_width * channels)
        if (actual_sample_rate * sample_width * channels) > 0
        else 0
    )

    provider_capabilities = {}
    if hasattr(provider, "get_capabilities_dict"):
        provider_capabilities = provider.get_capabilities_dict()

    # Batch providers bill by audio duration, silence included — cut long
    # silences with the local VAD before sending, and map timestamps back to
    # the original timeline afterwards (see utils/silence_condense.py).
    condense_map = None
    condensed_pcm, mapping, speech_seconds = condense_silence(
        pcm_data, actual_sample_rate, channels, sample_width
    )
    no_speech = mapping is not None and not mapping
    if not no_speech and mapping is None and speech_seconds is None:
        # condense_silence never scored the audio (clip under MIN_AUDIO_SECONDS
        # or VAD failure). With the audio-filtering gate on, still require
        # speech before paying for transcription. Only a conclusive silent/empty
        # result rejects; unscored audio fails open.
        if require_speech_for_transcription():
            speech_detection = detect_speech_pcm(
                pcm_data,
                actual_sample_rate,
                channels,
                sample_width,
            )
            no_speech = speech_detection.should_reject
    if no_speech:
        logger.info(
            f"🔇 No speech detected in [{start_time:.1f}s - {end_time or 'end'}] "
            f"of {conversation_id[:8]} — skipping paid transcription entirely"
        )
        return {
            "text": "",
            "segments": [],
            "words": [],
            "provider_name": provider.name,
            "provider_capabilities": provider_capabilities,
            "wav_size": 0,
            "sample_rate": actual_sample_rate,
        }
    if mapping is not None:
        condense_map = mapping
        pcm_data = condensed_pcm
        wav_data = _build_wav(pcm_data, actual_sample_rate, channels, sample_width)
        duration = (
            len(pcm_data) / (actual_sample_rate * sample_width * channels)
            if (actual_sample_rate * sample_width * channels) > 0
            else 0
        )

    if duration <= BATCH_CHUNK_SECONDS:
        # Single chunk — transcribe directly
        transcribe_kwargs: dict = {
            "audio_data": wav_data,
            "sample_rate": actual_sample_rate,
            "diarize": diarize,
        }
        if progress_callback:
            transcribe_kwargs["progress_callback"] = progress_callback
        if context_info:
            transcribe_kwargs["context_info"] = context_info

        try:
            result = await provider.transcribe(**transcribe_kwargs)
        except ConnectionError as e:
            raise RuntimeError(str(e))
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Transcription failed ({type(e).__name__}): {e}")

        result = await _align_result_words(result, wav_data)
        if condense_map:
            result = remap_condensed_result(result, condense_map)
        return {
            "text": result.get("text", ""),
            "segments": result.get("segments", []),
            "words": result.get("words", []),
            "provider_name": provider.name,
            "provider_capabilities": provider_capabilities,
            "wav_size": len(wav_data),
            "sample_rate": actual_sample_rate,
        }

    # Multi-chunk: split PCM, create WAV per chunk, transcribe, merge
    chunk_size_bytes = int(
        BATCH_CHUNK_SECONDS * actual_sample_rate * sample_width * channels
    )
    all_text = []
    all_segments = []
    all_words = []
    total_wav_size = 0
    chunk_num = 0

    logger.info(
        f"📦 Audio duration {duration:.0f}s exceeds {BATCH_CHUNK_SECONDS}s, "
        f"splitting into {BATCH_CHUNK_SECONDS}s chunks"
    )

    for i in range(0, len(pcm_data), chunk_size_bytes):
        chunk_pcm = pcm_data[i : i + chunk_size_bytes]
        chunk_wav = _build_wav(chunk_pcm, actual_sample_rate, channels, sample_width)
        chunk_start = i / (actual_sample_rate * sample_width * channels)
        chunk_num += 1

        logger.info(
            f"📦 Transcribing chunk {chunk_num}: "
            f"[{chunk_start:.0f}s - {chunk_start + len(chunk_pcm) / (actual_sample_rate * sample_width * channels):.0f}s]"
        )

        transcribe_kwargs = {
            "audio_data": chunk_wav,
            "sample_rate": actual_sample_rate,
            "diarize": diarize,
        }
        if progress_callback:
            transcribe_kwargs["progress_callback"] = progress_callback
        if context_info:
            transcribe_kwargs["context_info"] = context_info

        try:
            result = await provider.transcribe(**transcribe_kwargs)
        except ConnectionError as e:
            raise RuntimeError(str(e))
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Transcription failed ({type(e).__name__}): {e}")

        result = await _align_result_words(result, chunk_wav)
        # Offset timestamps by chunk start time
        for seg in result.get("segments", []):
            seg["start"] = seg.get("start", 0.0) + chunk_start
            seg["end"] = seg.get("end", 0.0) + chunk_start
        for w in result.get("words", []):
            w["start"] = w.get("start", 0.0) + chunk_start
            w["end"] = w.get("end", 0.0) + chunk_start

        all_text.append(result.get("text", ""))
        all_segments.extend(result.get("segments", []))
        all_words.extend(result.get("words", []))
        total_wav_size += len(chunk_wav)

    merged = {
        "text": " ".join(all_text),
        "segments": all_segments,
        "words": all_words,
        "provider_name": provider.name,
        "provider_capabilities": provider_capabilities,
        "wav_size": total_wav_size,
        "sample_rate": actual_sample_rate,
    }
    # Chunk offsets above are condensed-timeline; map back to original time.
    if condense_map:
        remap_condensed_result(merged, condense_map)
    return merged


async def process_transcription_result(
    conversation_id: str,
    version_id: str,
    trigger: str,
    transcript_text: str,
    segments: list,
    words: list,
    provider_name: str,
    provider_capabilities: dict,
    wav_size: int,
    processing_time: float,
    user_id: str | None = None,
    client_id: str | None = None,
) -> dict:
    """
    Post-transcription processing: plugin dispatch, speech validation, segment processing,
    transcript version creation, DB save, job metadata update.

    Returns:
        Dict with processing results including transcript data for downstream jobs
    """
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        raise ValueError(f"Conversation {conversation_id} not found")

    if user_id is None:
        user_id = str(conversation.user_id) if conversation.user_id else None
    if client_id is None:
        client_id = (
            conversation.client_id if hasattr(conversation, "client_id") else None
        )

    # The provider response has already crossed the content-hash cache boundary in
    # RegistryBatchTranscriptionProvider.transcribe(). Validate a copy before any
    # plugin sees it or it becomes active: cache the paid result, never cache corruption
    # into the conversation timeline.
    try:
        audio_ranges = await load_transcript_audio_ranges(conversation_id)
        segments, words = validate_and_normalize_transcript_timing(
            segments,
            words,
            audio_duration=conversation.audio_total_duration or 0.0,
            audio_ranges=audio_ranges,
        )
    except TranscriptTimingError as error:
        reason = f"{error.code}: {error}"
        conversation.transcript_integrity_error = reason
        await conversation.save()
        details = {
            **error.details,
            "provider": provider_name,
            "trigger": trigger,
            "version_id": version_id,
        }
        record_event_sync(
            severity="error",
            category="data_integrity",
            source="transcription_ingest",
            title="Transcript timing rejected",
            detail=reason,
            user_id=user_id,
            client_id=client_id,
            conversation_id=conversation_id,
            metadata=details,
            incident_key=f"transcript-integrity:{conversation_id}",
        )
        logger.warning(
            "Rejected %s transcript for %s before activation: %s",
            provider_name,
            conversation_id,
            reason,
        )
        raise
    conversation.transcript_integrity_error = None

    # Guard: a batch / re-transcription that comes back without usable structure
    # (no words AND no segments) must NOT replace a good existing transcript.
    # nemo batch occasionally returns bare text with no word/segment timing; promoting
    # it as the active version silently drops diarization and playback alignment from
    # the streaming version that was already there. Skip instead of clobbering — only
    # real content (with timing) should become the active version. An actual provider
    # error is a different path (it raises upstream); an empty result is not an error.
    new_has_text = bool((transcript_text or "").strip())
    new_has_structure = bool(words) or bool(segments)
    existing = conversation.active_transcript
    existing_is_populated = bool(
        existing
        and (existing.words or existing.segments)
        and (existing.transcript or "").strip()
    )
    if (not new_has_text or not new_has_structure) and existing_is_populated:
        logger.warning(
            f"⚠️ {provider_name} '{trigger}' for {conversation_id} returned an empty/"
            f"contentless transcript (text={new_has_text}, words={len(words)}, "
            f"segments={len(segments)}); keeping existing active version "
            f"'{existing.version_id}' instead of replacing it."
        )
        return {
            "success": False,
            "skipped": True,
            "reason": "empty_or_contentless_transcription",
            "conversation_id": conversation_id,
            "version_id": version_id,
            "kept_active_version": existing.version_id,
        }

    # Trigger transcript-level plugins BEFORE speech validation
    if transcript_text and not conversation.memory_excluded:
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
    elif transcript_text:
        logger.info(
            f"Skipping transcript plugins for memory-excluded conversation {conversation_id[:8]}"
        )

    # Validate meaningful speech
    transcript_data = {"text": transcript_text, "words": words}
    speech_analysis = analyze_speech(transcript_data)

    if not speech_analysis.get("has_speech", False):
        logger.warning(
            f"⚠️ No meaningful speech for conversation {conversation_id}: "
            f"{speech_analysis.get('reason', 'unknown')}"
        )
        await mark_conversation_deleted(
            conversation_id=conversation_id,
            deletion_reason="no_meaningful_speech_batch_transcription",
        )
        await _settle_audio_evidence_span(
            conversation,
            int(speech_analysis.get("word_count", 0)),
            "no_speech",
        )

        # Cancel dependent jobs
        current_job = get_current_job()
        if current_job:
            # Include event_complete: it depends on the memory/summary bundle, and
            # cancelling those (enqueue_dependents=False) would otherwise strand the
            # finalizer in the deferred registry forever. mark_conversation_deleted
            # already settled the status, so the finalizer is redundant here.
            job_patterns = [
                f"crop_{conversation_id[:12]}",
                f"speaker_{conversation_id[:12]}",
                f"memory_{conversation_id[:12]}",
                f"title_{conversation_id[:12]}",
                f"short_summary_{conversation_id[:12]}",
                f"detailed_summary_{conversation_id[:12]}",
                f"event_complete_{conversation_id[:12]}",
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
                        logger.debug(f"Job {job_id} hash not found")
                    else:
                        logger.debug(
                            f"Job {job_id} not found or already completed: {e}"
                        )

            if cancelled_jobs:
                logger.info(
                    f"🚫 Cancelled {len(cancelled_jobs)} dependent jobs due to no meaningful speech"
                )

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

    # Get provider capabilities for downstream processing decisions
    provider_has_diarization = provider_capabilities.get("diarization", False)

    # Build speaker segments
    speaker_segments = []
    diarization_source = None
    segments_created_by = "speaker_service"

    if segments:
        speaker_segments = []
        for seg in segments:
            raw_speaker = seg.get("speaker")
            if raw_speaker is None:
                speaker = "Speaker 0"
            elif isinstance(raw_speaker, int):
                speaker = f"Speaker {raw_speaker}"
            else:
                speaker = str(raw_speaker)

            text = seg.get("text", "")
            classification = classify_segment_text(text)
            seg_type = "speech"
            if classification == "event":
                seg_type = "event"
                speaker = ""

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
            diarization_source = "provider"
            segments_created_by = "provider_diarization"
        else:
            segments_created_by = "provider"
    elif transcript_text and transcript_text.strip():
        # No provider segments: emit a single Speaker 0 segment spanning the whole
        # transcript so any non-empty result is always renderable in the UI (mirrors
        # the streaming path). Use word timings when present, else span 0..end. If
        # speaker recognition is enabled, recognize_speakers_job refines this later —
        # but we no longer *rely* on it (a word-less batch result, e.g. nemotron's
        # prompt-model decode, used to leave the conversation with 0 segments).
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

    # Add new transcript version
    provider_normalized = provider_name.lower() if provider_name else "unknown"

    word_objects = [
        Conversation.Word(
            word=w.get("word", ""),
            start=w.get("start", 0.0),
            end=w.get("end", 0.0),
            confidence=w.get("confidence"),
        )
        for w in words
    ]

    metadata = {
        "trigger": trigger,
        "audio_file_size": wav_size,
        "word_count": len(words),
        "segments_created_by": segments_created_by,
        "provider_capabilities": provider_capabilities,
    }

    new_version = conversation.add_transcript_version(
        version_id=version_id,
        transcript=transcript_text,
        words=word_objects,
        segments=speaker_segments,
        provider=provider_normalized,
        model=provider_name,
        processing_time_seconds=processing_time,
        metadata=metadata,
        set_as_active=True,
    )

    if diarization_source:
        new_version.diarization_source = diarization_source

    if not transcript_text or len(transcript_text.strip()) == 0:
        conversation.title = TITLE_NOT_GENERATED
        conversation.summary = "No speech detected"

    await conversation.save()
    await _settle_audio_evidence_span(conversation, len(words), "transcribed")

    # Trim the conversation's silence now that its transcript is attached and active.
    # This is where continuous capture lands: a 30-minute ScreenPipe recording is
    # mostly silence, and it must be trimmed BEFORE the post-conversation chain so
    # speaker recognition reads the same timeline the audio now has. The trim re-times
    # the active version in place, so re-read it for the result below.
    # Lazy import: circular dependency — conversation_jobs imports transcription_jobs.
    from advanced_omi_backend.workers.conversation_jobs import maybe_trim_silence

    if await maybe_trim_silence(conversation_id) is not None:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        trimmed = next(
            (
                v
                for v in (conversation.transcript_versions if conversation else [])
                if v.version_id == version_id
            ),
            None,
        )
        if trimmed is not None:
            speaker_segments = trimmed.segments or []
            words = [word.model_dump() for word in (trimmed.words or [])]
            transcript_text = trimmed.transcript or ""

    logger.info(
        f"✅ Transcript processing completed for {conversation_id} in {processing_time:.2f}s"
    )

    update_job_meta(
        conversation_id=conversation_id,
        title=conversation.title,
        summary=conversation.summary,
        transcript_length=len(transcript_text),
        word_count=len(words),
        processing_time=processing_time,
    )

    result = {
        "success": True,
        "conversation_id": conversation_id,
        "version_id": version_id,
        "audio_source": "mongodb_chunks",
        "transcript": transcript_text,
        "segments": [seg.model_dump() for seg in speaker_segments],
        "words": words,
        "provider": provider_name,
        "provider_capabilities": provider_capabilities,
        "diarization_source": diarization_source,
        "processing_time_seconds": processing_time,
        "trigger": trigger,
    }
    set_trace_io(
        output={
            "transcript": transcript_text,
            "word_count": len(words),
            "segment_count": len(speaker_segments),
            "provider": provider_name,
        }
    )
    return result


@async_job(redis=True, beanie=True)
@traced_job("transcription", pipeline_stage="transcription")
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
    set_otel_session(conversation_id)
    logger.info(
        f"🔄 RQ: Starting transcript processing for conversation {conversation_id} (trigger: {trigger})"
    )

    start_time_wall = time.time()

    # Get the conversation for user context
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        raise ValueError(f"Conversation {conversation_id} not found")

    user_id = str(conversation.user_id) if conversation.user_id else None
    client_id = conversation.client_id if hasattr(conversation, "client_id") else None
    set_span_attrs(user_id=user_id, client_id=client_id)
    set_trace_io(
        input={
            "conversation_id": conversation_id,
            "version_id": version_id,
            "trigger": trigger,
        }
    )

    # Build ASR context
    context_info = None
    try:
        asr_ctx = await gather_transcription_context(user_id=user_id)
        context_info = asr_ctx.combined

        # Log ASR context as span attributes
        set_span_attrs(
            asr_hot_words=asr_ctx.hot_words[:200] if asr_ctx.hot_words else "",
            asr_user_jargon=asr_ctx.user_jargon[:200] if asr_ctx.user_jargon else "",
            asr_context_length=len(context_info),
        )
    except Exception as e:
        logger.warning(f"Failed to build ASR context: {e}")

    # Progress callback for RQ job metadata.
    # RQ job_timeout=-1 disables the parent-side kill, so the job runs as long
    # as the ASR service keeps sending progress. Application-level staleness is
    # handled by httpx read_timeout on the NDJSON stream (no progress → socket
    # timeout → exception → job fails naturally).
    last_progress_time = [start_time_wall]  # mutable ref for closure

    def _on_batch_progress(event: dict) -> None:
        last_progress_time[0] = time.time()
        job = get_current_job()
        if job:
            current = event.get("current", 0)
            total = event.get("total", 0)
            batch_progress = {
                "current": current,
                "total": total,
                "percent": int(current / total * 100) if total else 0,
                "message": f"Transcribing segment {current} of {total}",
            }
            job.meta["batch_progress"] = batch_progress
            job.save_meta()

            # Push batch progress to frontend via SSE (throttled to every 3s)
            if user_id:
                publish_sse_event_throttled(
                    user_id,
                    "job.progress",
                    {
                        "conversation_id": conversation_id,
                        "job_type": "transcribe_full_audio_job",
                        "batch_progress": batch_progress,
                    },
                )

    # Transcribe full audio
    try:
        result = await transcribe_audio_range(
            conversation_id=conversation_id,
            diarize=True,
            context_info=context_info,
            progress_callback=_on_batch_progress,
        )
    except ValueError as e:
        raise FileNotFoundError(
            f"No audio chunks found for conversation {conversation_id}: {e}"
        )
    except Exception as e:
        logger.error(f"Transcription failed for {conversation_id}: {e}", exc_info=True)
        raise

    processing_time = time.time() - start_time_wall

    return await process_transcription_result(
        conversation_id=conversation_id,
        version_id=version_id,
        trigger=trigger,
        transcript_text=result["text"],
        segments=result["segments"],
        words=result["words"],
        provider_name=result["provider_name"],
        provider_capabilities=result["provider_capabilities"],
        wav_size=result["wav_size"],
        processing_time=processing_time,
        user_id=user_id,
        client_id=client_id,
    )


async def create_audio_only_conversation(
    session_id: str, user_id: str, client_id: str
) -> "Conversation":
    """Resolve the session's required durable owner for batch transcription."""
    placeholder_conversation = await Conversation.find_one(
        Conversation.source_session_id == session_id,
        Conversation.always_persist == True,
        In(
            Conversation.processing_status,
            # active = in-flight placeholder, failed = prior failure to retry/attach
            [
                Conversation.ConversationStatus.ACTIVE.value,
                Conversation.ConversationStatus.FAILED.value,
            ],
        ),
    )

    if placeholder_conversation:
        logger.info(
            f"✅ Found always_persist placeholder conversation {placeholder_conversation.conversation_id[:12]} "
            f"for session {session_id[:12]}, reusing for batch transcription"
        )
        # In-flight; the finalizer/reconciler owns the terminal status.
        placeholder_conversation.processing_status = (
            Conversation.ConversationStatus.ACTIVE.value
        )
        placeholder_conversation.title = TITLE_NOT_GENERATED
        placeholder_conversation.summary = (
            "Processing audio with offline transcription..."
        )
        await placeholder_conversation.save()

        # Audio chunks are already linked to this conversation_id
        # (stored by audio_streaming_persistence_job)
        return placeholder_conversation

    raise RuntimeError(
        f"Session {session_id} has no durable conversation owner; refusing to "
        "invent an alternate batch owner"
    )


@async_job(redis=True, beanie=True)
async def transcription_fallback_check_job(
    session_id: str,
    user_id: str,
    client_id: str,
    conversation_id: str | None = None,
    timeout_seconds: int | None = None,
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
        conversation_id: Specific conversation ID to check (avoids matching old conversations)
        timeout_seconds: Max wait time for batch transcription (default 2 minutes)
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with status (pass_through or batch_fallback_completed) and conversation details
    """
    if timeout_seconds is None:
        timeout_seconds = get_streaming_fallback_timeout()

    logger.info(f"🔍 Checking transcription status for session {session_id[:12]}")

    # Find the exact conversation if known, otherwise resolve by immutable session.
    if conversation_id:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
    else:
        conversation = await Conversation.find_one(
            Conversation.source_session_id == session_id,
            Conversation.always_persist == True,
            In(
                Conversation.processing_status,
                # active = in-flight placeholder, failed = prior failure to retry/attach
                [
                    Conversation.ConversationStatus.ACTIVE.value,
                    Conversation.ConversationStatus.FAILED.value,
                ],
            ),
        )

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

    # Ensure a conversation exists to attach the batch transcript to.
    if not conversation:
        conversation = await create_audio_only_conversation(
            session_id, user_id, client_id
        )

    conv_id = conversation.conversation_id

    # Ensure audio chunks exist for THIS conversation before transcribing. The
    # streaming persistence job stores chunks under the conversation_id as audio
    # arrives, so by the time we get here they should be in MongoDB. Zero chunks
    # means no audio was captured for this conversation — skip gracefully instead of
    # crashing on "No audio chunks found" (which would fail the RQ job and leave its
    # dependents deferred forever).
    chunks_count = await AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conv_id
    ).count()

    if chunks_count == 0:
        logger.info(
            f"ℹ️ No audio chunks for conversation {conv_id[:12]} "
            f"(session {session_id[:12]}). Session ended without usable audio. "
            f"Skipping batch fallback."
        )
        # Terminal dead-end: no audio and no transcript will ever exist. This is the
        # one place no post-conversation chain (and thus no finalizer) runs, so settle
        # the status here.
        if conversation.apply_status(settled=True):
            await conversation.save()
        return {
            "status": "skipped",
            "reason": "no_audio",
            "message": "No audio available for batch transcription",
            "session_id": session_id,
            "conversation_id": conv_id,
        }

    logger.info(
        f"✅ Found {chunks_count} MongoDB chunks for conversation {conv_id[:12]}, "
        f"proceeding with batch transcription"
    )

    # Transcribe directly — transcribe_audio_range handles chunking internally
    version_id = f"batch_fallback_{session_id[:12]}"

    # Build ASR context
    context_info = None
    try:
        context_info = await get_asr_context(user_id=user_id)
    except Exception as e:
        logger.warning(f"Failed to build ASR context: {e}")

    start_time_wall = time.time()

    result = await transcribe_audio_range(
        conv_id, diarize=True, context_info=context_info
    )

    processing_time = time.time() - start_time_wall

    processing_result = await process_transcription_result(
        conversation_id=conv_id,
        version_id=version_id,
        trigger="batch_fallback",
        transcript_text=result["text"],
        segments=result["segments"],
        words=result["words"],
        provider_name=result["provider_name"],
        provider_capabilities=result["provider_capabilities"],
        wav_size=result["wav_size"],
        processing_time=processing_time,
        user_id=user_id,
        client_id=client_id,
    )

    # If no meaningful speech, conversation was marked deleted — skip post-processing
    if processing_result.get("deleted"):
        logger.info(
            f"🗑️ Batch fallback found no meaningful speech for {conv_id[:12]}, "
            f"skipping post-conversation jobs"
        )
        return {
            "status": "batch_fallback_no_speech",
            "transcript_source": "batch",
            "conversation_id": conv_id,
            "reason": processing_result.get("reason"),
        }

    # Trim silence before post-processing. This is the batch-fallback finalization
    # path (no streaming speech was detected mid-session, so open_conversation_job's
    # Phase 6b never ran); the same trim must happen here so an always_persist
    # conversation that recorded a long pause before speech still gets the silence
    # split off. Best-effort — never blocks the chain.
    # Lazy import: circular dependency — conversation_jobs imports transcription_jobs.
    from advanced_omi_backend.workers.conversation_jobs import maybe_trim_silence

    await maybe_trim_silence(conv_id)

    # Enqueue post-conversation jobs
    post_jobs = start_post_conversation_jobs(
        conversation_id=conv_id,
        user_id=user_id,
        transcript_version_id=version_id,
        depends_on_job=None,
        client_id=client_id,
        end_reason=Conversation.EndReason.WEBSOCKET_DISCONNECT.value,
        trigger=Conversation.ProcessingTrigger.LIVE_SESSION.value,
    )

    logger.info(
        f"📋 Enqueued {len(post_jobs)} post-conversation jobs for "
        f"batch fallback conversation {conv_id[:12]}"
    )

    return {
        "status": "batch_fallback_completed",
        "transcript_source": "batch",
        "conversation_id": conv_id,
        "post_job_ids": post_jobs,
    }


async def _transcription_failure_context(
    store: "SessionStore",
    aggregator: "TranscriptionResultsAggregator",
    session_id: str,
    client_id: str,
) -> str:
    """Gather actionable diagnostics for a transcription-failure system event.

    The bare failure logs (e.g. "no transcription activity") are symptoms; the real
    cause usually lives in the streaming consumer's ``transcription_error`` flag (the
    provider exception), or — when the provider connected but produced nothing — in
    the configured provider name and chunk count. Returning all of it as a multi-line
    block means the System Errors page row carries the cause, not just the symptom.
    """
    lines: list[str] = []

    # The real upstream exception, if the streaming consumer recorded one.
    try:
        upstream = await store.get_transcription_error(session_id)
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        upstream = None
    if upstream:
        lines.append(f"   Provider error: {upstream}")

    # Which streaming provider was configured (so it's clear what to check).
    try:
        registry = get_models_registry()
        stream_model = registry.get_default("stt_stream") if registry else None
        provider_name = stream_model.name if stream_model else "none configured"
    except Exception:  # noqa: BLE001
        provider_name = "unknown"
    lines.append(f"   Streaming provider: {provider_name}")

    # Whether any audio was actually transcribed.
    try:
        combined = await aggregator.get_combined_results(session_id)
        chunk_count = combined.get("chunk_count", 0)
    except Exception:  # noqa: BLE001
        chunk_count = "unknown"
    lines.append(f"   Transcribed chunks: {chunk_count}")

    try:
        view = await store.read(session_id)
    except Exception:  # noqa: BLE001
        view = None
    if view:
        lines.append(
            f"   Provider connection: {view.transcription_provider_status or 'unknown'}"
        )
        lines.append(
            f"   Last audio sent: {view.transcription_last_audio_sent_at or 'never'}"
        )
        lines.append(
            f"   Last provider message: {view.transcription_last_message_at or 'never'}"
        )

    lines.append(f"   session={session_id} client={client_id}")
    return "\n".join(lines)


async def _session_ended_by_disconnect(store: "SessionStore", session_id: str) -> bool:
    """True when a zero-transcription session ended because the client dropped its
    WebSocket (e.g. the device walked out of network range) rather than because the
    transcription provider failed.

    A provider exception recorded on the session always wins — that is a genuine
    service fault regardless of how the socket closed. Only a clean
    ``websocket_disconnect`` with no recorded provider error is treated as benign, so
    it is logged at WARNING (not ERROR) and never raises a system-error event — a user
    walking through a dead zone shouldn't fill the System Errors page.
    """
    try:
        if await store.get_transcription_error(session_id):
            return False
        return (await store.get_completion_reason(session_id)) == "websocket_disconnect"
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return False


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

    set_otel_session(session_id)
    logger.info(f"🔍 Starting speech detection for session {session_id[:12]}")

    # Setup
    aggregator = TranscriptionResultsAggregator(redis_client)
    current_job = get_current_job()
    store = SessionStore(redis_client)
    start_time = time.time()
    max_runtime = 7200  # 2 hours — sufficient gap between conversations; fresh job re-enqueued after each

    # Get conversation count
    conversation_count = await store.get_conversation_count(session_id)

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

    # Live-transcription mode. In "off" mode there is no streaming/windowed worker
    # filling the aggregator, so zero results is the expected steady state — not a
    # failure. The final transcript is produced by batch transcription when the
    # session ends (the "session ended without speech" path enqueues the fallback
    # on the full audio). Disable the no-activity watchdog in that case so it does
    # not abort a still-recording session at the 60s mark.
    live_segmentation = get_live_segmentation()
    expects_live_results = live_segmentation != "off"

    # Track when session closes for graceful shutdown
    session_closed_at = None
    final_check_grace_period = (
        15  # Wait up to 15 seconds for final transcription after session closes
    )
    last_speech_analysis = None  # Track last analysis for detailed logging
    max_runtime_reached = False  # Distinguish the max-runtime exit from session close
    no_activity_warning_logged = False

    # Main loop: Listen for speech
    while True:
        # Check if job still exists in Redis (detect zombie state)
        if not await check_job_alive(redis_client, current_job, session_id):
            break

        # Early transcription failure detection — check every iteration, not just grace period
        error_status = await store.get_transcription_error(session_id)
        if error_status:
            logger.error(f"❌ Transcription error detected: {error_status}")
            break

        # No-activity watchdog: if 60s elapsed with zero transcription results, the
        # live provider is down. Only meaningful when we actually expect live results
        # — in "off" mode nothing fills the aggregator by design, so skip it and let
        # the loop run until the session closes (full-audio batch fallback then runs).
        elapsed = time.time() - start_time
        if (
            expects_live_results
            and elapsed > 60
            and not session_closed_at
            and not no_activity_warning_logged
        ):
            watchdog_combined = await aggregator.get_combined_results(session_id)
            if not watchdog_combined.get("chunk_count", 0):
                # Only a real provider fault when audio is flowing in but the streaming
                # consumer produces nothing. A connected-but-quiet device (e.g. speech
                # detection just re-armed after a conversation ended and the user hasn't
                # spoken again) sends no audio, so zero results is expected — firing here
                # mislabels idle as "transcription service did not respond". When idle,
                # fall through and keep listening until audio resumes or the session
                # closes (the grace-period / disconnect handling below then applies).
                last_chunk_at = await store.get_last_chunk_at(session_id)
                audio_idle = (
                    last_chunk_at is None
                    or (time.time() - last_chunk_at) > AUDIO_INFLOW_IDLE_SECONDS
                )
                if not audio_idle:
                    diag = await _transcription_failure_context(
                        store, aggregator, session_id, client_id
                    )
                    logger.warning(
                        f"⚠️ No transcription activity after {elapsed:.0f}s — "
                        f"provider may be connected and awaiting recognizable speech\n"
                        f"{diag}"
                    )
                    no_activity_warning_logged = True

        # Check if session has closed
        session_closed = await store.get_status(session_id) in (
            SessionStatus.FINALIZING,
            SessionStatus.FINISHED,
        )

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
        close_reason_str = await store.take_close_request(session_id)
        if close_reason_str:
            logger.info(
                f"🔒 Conversation close requested ({close_reason_str}) during speech detection — "
                f"no open conversation to close, flag consumed"
            )

        if time.time() - start_time > max_runtime:
            logger.warning(f"⏱️ Max runtime reached, exiting")
            max_runtime_reached = True
            break

        # Get transcription results
        combined = await aggregator.get_combined_results(session_id)
        if not combined["text"]:
            # Health check: detect transcription errors early during grace period
            if session_closed_at:
                # Check for streaming consumer errors in session metadata
                error_status = await store.get_transcription_error(session_id)
                if error_status:
                    error_msg = error_status
                    logger.error(f"❌ Transcription service error: {error_msg}")
                    logger.error(
                        f"❌ Session failed - transcription service unavailable"
                    )
                    break

                # Check if we've been waiting too long with no results at all.
                # Only an error when we expect live results — in "off" mode no live
                # activity is expected, so fall through to the normal grace-period
                # exit and let the batch fallback transcribe the full audio.
                grace_elapsed = time.time() - session_closed_at
                if (
                    expects_live_results
                    and grace_elapsed > 5
                    and not combined.get("chunk_count", 0)
                ):
                    # A healthy provider may legitimately produce no messages for
                    # silence/noise, so lack of transcript alone is not a service error.
                    diag = await _transcription_failure_context(
                        store, aggregator, session_id, client_id
                    )
                    if await _session_ended_by_disconnect(store, session_id):
                        logger.warning(
                            f"⚠️ Session ended by client disconnect before any "
                            f"transcription (after {grace_elapsed:.1f}s) — likely a "
                            f"network drop, not a service fault\n{diag}"
                        )
                    else:
                        logger.warning(
                            f"⚠️ Session ended without transcription\n"
                            f"   No transcription activity after {grace_elapsed:.1f}s "
                            f"of finalization grace\n"
                            f"{diag}"
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
        await store.record_event(session_id, "speech_detected")
        await store.set_speech_detected_at(session_id)

        # Step 2: If speaker filter enabled, check for enrolled speakers
        identified_speakers = []
        speaker_check_job = None  # Initialize for later reference
        if speaker_filter_enabled:
            logger.info(f"🎤 Enqueuing speaker check job...")

            # Add session event for speaker check starting
            await store.record_event(session_id, "speaker_check_starting")
            await store.set_speaker_check(session_id, SpeakerCheckStatus.CHECKING)

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
                    await store.record_event(session_id, "speaker_check_complete")
                    await store.set_speaker_check(
                        session_id,
                        (
                            SpeakerCheckStatus.ENROLLED
                            if enrolled_present
                            else SpeakerCheckStatus.NOT_ENROLLED
                        ),
                    )
                    if identified_speakers:
                        await store.set_identified_speakers(
                            session_id, identified_speakers
                        )
                    break
                elif speaker_check_job.is_failed:
                    logger.warning(
                        f"⚠️ Speaker check job failed, assuming not enrolled"
                    )

                    # Update session event for speaker check failed
                    await store.record_event(session_id, "speaker_check_failed")
                    await store.set_speaker_check(session_id, SpeakerCheckStatus.FAILED)
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
                await store.record_event(session_id, "speaker_check_timeout")
                await store.set_speaker_check(session_id, SpeakerCheckStatus.TIMEOUT)

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

        # Enqueue conversation job with speech detection job ID
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
    if last_speech_analysis:
        reason = last_speech_analysis.get("reason", "No transcription received")
    elif not expects_live_results:
        # "off" mode: no live transcription by design — the batch fallback below
        # transcribes the full audio. This is the normal path, not a failure.
        reason = "Live transcription disabled (off mode) — batch transcription pending"
    else:
        reason = "No transcription received"

    # No transcript is not itself a provider failure: streaming APIs may emit no
    # messages for silence/noise. Only an explicit transport/provider error is ERROR.
    if reason == "No transcription received":
        diag = await _transcription_failure_context(
            store, aggregator, session_id, client_id
        )
        provider_error = await store.get_transcription_error(session_id)
        if provider_error:
            logger.error(
                f"❌ Session failed - transcription provider error\n"
                f"   Reason: {provider_error}\n"
                f"   Runtime: {time.time() - start_time:.1f}s\n"
                f"{diag}"
            )
        elif await _session_ended_by_disconnect(store, session_id):
            logger.warning(
                f"⚠️ Session ended by client disconnect with no transcription "
                f"(runtime {time.time() - start_time:.1f}s) — likely a network drop, "
                f"not a service fault\n{diag}"
            )
        else:
            logger.warning(
                f"⚠️ Session ended without transcription\n"
                f"   Reason: {reason}\n"
                f"   Runtime: {time.time() - start_time:.1f}s\n"
                f"{diag}"
            )
    else:
        logger.info(
            f"✅ Session ended, deferring to batch transcription\n"
            f"   Reason: {reason}\n"
            f"   Runtime: {time.time() - start_time:.1f}s"
        )

    # Guard: never fail a conversation that is still being recorded.
    #
    # If we reached this exit while the session is still ACTIVE (e.g. the
    # no-activity watchdog or a transcription-error break fired mid-session, or a
    # duplicate/stale detector lost the race), the always_persist placeholder
    # found below may be the *currently-recording* conversation. Marking it
    # transcription_failed here — and batch-transcribing only the partial audio
    # captured so far — orphans the rest of the audio that is still streaming in.
    # This was the root cause of conversations being marked failed ~2 minutes in
    # while 10+ more minutes of audio kept arriving.
    #
    # Instead, leave the placeholder untouched and re-arm a single listener
    # (single-flight). When the session genuinely closes, the listener exits via
    # the session-closed path (status FINALIZING/FINISHED) and the failure +
    # full-audio batch fallback below run correctly on the complete recording.
    #
    # Off-mode (expects_live_results=False) is excluded: it has no live worker, so
    # zero results is normal and its window-rotation logic below must still run.
    if expects_live_results and not max_runtime_reached:
        session_status = await store.get_status(session_id)
        if session_status == SessionStatus.ACTIVE:
            logger.warning(
                f"⚠️ Speech detection for {session_id[:12]} exited while session "
                f"still ACTIVE (reason: {reason}). Not failing the live placeholder; "
                f"re-arming listener and deferring to session-close handling."
            )
            # Brief backoff so a persistent provider error can't spin a tight
            # re-arm loop; single-flight keeps this to one listener regardless.
            await asyncio.sleep(5)
            enqueue_speech_detection(
                session_id, user_id, client_id, reason="active_rearm"
            )
            return {
                "session_id": session_id,
                "user_id": user_id,
                "client_id": client_id,
                "status": "rearmed_session_active",
                "reason": reason,
                "runtime_seconds": time.time() - start_time,
            }

    # Check if this is an always_persist conversation that needs to be marked as failed
    # NOTE: We check MongoDB directly because the conversation:current Redis key might have been
    # deleted by the audio persistence job cleanup (which runs in parallel).
    logger.info(
        f"🔍 Checking MongoDB for always_persist conversation with client_id: {client_id}"
    )

    # Resolve by immutable capture session, never by device id. A reconnect can
    # have an older session draining while a new session for the same client runs.
    conversation = await Conversation.find_one(
        Conversation.source_session_id == session_id,
        Conversation.always_persist == True,
        Conversation.processing_status == Conversation.ConversationStatus.ACTIVE.value,
    )

    if conversation:
        # Do NOT mark failed here: the batch fallback enqueued below may still
        # recover a transcript. Stamping "failed" pre-emptively is exactly the bug
        # that left recovered conversations stuck. The status is owned by the
        # finalizer (post-conv chain) and the fallback's no-audio dead-end; both
        # call apply_status(), so a real failure still settles to "failed".
        logger.info(
            f"🔎 Found always_persist placeholder {conversation.conversation_id} for "
            f"session {session_id[:12]} with no live transcript; deferring status to "
            f"batch fallback (reason so far: {reason})"
        )
    else:
        logger.info(
            f"ℹ️ No always_persist placeholder conversation found for session {session_id[:12]}"
        )

    # Enqueue fallback check job for failed streaming sessions
    # This will attempt batch transcription as a fallback
    config_timeout = get_streaming_fallback_timeout()
    conversation_id = conversation.conversation_id if conversation else None

    # The fallback reads audio chunks the audio-persistence job writes to MongoDB.
    # Those are separate jobs reacting to session-end independently, so the fallback
    # can race ahead and read 0 chunks before persistence has flushed them. Make the
    # ordering explicit via the RQ dependency graph instead of a tuned wait: the
    # fallback cannot start until the persistence job reaches a terminal state (its
    # final flush is durable). allow_failure=True so a persistence failure degrades to
    # the fallback's graceful "no audio → skip" path rather than deferring forever.
    #
    # Two guards:
    #   - Only depend on the persistence job while it's still live. If it already
    #     finished (or its key expired), the chunks are durable — depending on a
    #     missing job id would defer the fallback forever.
    #   - Skip the dependency in the off-mode rotation path (below), where the
    #     persistence job keeps running for the next window and would block for hours.
    fallback_depends_on = None
    is_rotation = max_runtime_reached and not expects_live_results
    if not is_rotation:
        try:
            persistence_job = Job.fetch(
                f"audio-persist_{session_id}",
                connection=transcription_queue.connection,
            )
            if persistence_job.get_status(refresh=True) not in (
                "finished",
                "failed",
                "stopped",
                "canceled",
            ):
                fallback_depends_on = Dependency(
                    jobs=[persistence_job], allow_failure=True
                )
        except NoSuchJobError:
            fallback_depends_on = None  # persistence done & expired — chunks durable

    fallback_job = transcription_queue.enqueue(
        transcription_fallback_check_job,
        session_id,
        user_id,
        client_id,
        conversation_id=conversation_id,
        timeout_seconds=config_timeout,
        job_timeout=config_timeout + 120,  # 2 min overhead for fallback check
        # Key the job per-conversation, not per-session. A shared
        # fallback_check_{session_id} id meant that when several conversations in
        # one session ended close together, RQ kept only one and silently dropped
        # the rest — so most failed placeholders never got a batch-transcription
        # fallback. Per-conversation ids let every conversation get its own.
        job_id=f"fallback_check_{conversation_id or session_id}",
        depends_on=fallback_depends_on,
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

    # Off-mode long-session rotation. We exited on the 2h max-runtime cap (not session
    # close), and "off" mode has no live worker to rotate conversations the way
    # streaming/windowed modes do. The fallback above transcribes the window we just
    # closed; if the session is still active, rotate to a fresh placeholder and
    # re-enqueue speech detection so the next window is transcribed too (otherwise
    # everything past 2h would never be transcribed).
    rotated_job_id = None
    if max_runtime_reached and not expects_live_results:
        status = await store.get_status(session_id)
        if status == SessionStatus.ACTIVE:
            next_count = await store.increment_conversation_count(session_id)

            # Rotate through the lifecycle module. Persistence no longer creates
            # Mongo conversations in response to an absent pointer; it waits for a
            # deliberate assignment so every placeholder has a finalization owner.
            async with store.conversation_create_lock(session_id):
                await store.clear_current_conversation(
                    session_id, expected_id=conversation_id
                )
            assignment = await ensure_active_session_placeholder(
                store,
                session_id=session_id,
                user_id=user_id,
                client_id=client_id,
            )
            if assignment is None:
                logger.info(
                    f"⏱️ off-mode rotation for {session_id[:12]} was overtaken by "
                    "session finalization — not re-enqueueing"
                )
                return {
                    "session_id": session_id,
                    "user_id": user_id,
                    "client_id": client_id,
                    "no_speech_detected": True,
                    "fallback_job_id": fallback_job.id,
                    "rotated_speech_detection_job_id": None,
                    "reason": reason,
                    "runtime_seconds": time.time() - start_time,
                }

            # replaces_current=True: this job IS the tracked live detector and is
            # deliberately handing off to its successor, so skip the single-flight
            # liveness check (which would otherwise see this still-running job).
            rotated_job_id = enqueue_speech_detection(
                session_id,
                user_id,
                client_id,
                reason=f"offmode_rotation_window_{next_count + 1}",
                replaces_current=True,
            )
            logger.info(
                f"🔄 off-mode: hit {max_runtime}s cap — rotated placeholder and "
                f"re-enqueued speech detection {rotated_job_id} for active session "
                f"{session_id[:12]} (window #{next_count + 1})"
            )
        else:
            logger.info(
                f"⏱️ off-mode: hit {max_runtime}s cap but session "
                f"status={status.value if status else None} — not re-enqueueing"
            )

    return {
        "session_id": session_id,
        "user_id": user_id,
        "client_id": client_id,
        "no_speech_detected": True,
        "fallback_job_id": fallback_job.id,
        "rotated_speech_detection_job_id": rotated_job_id,
        "reason": reason,
        "runtime_seconds": time.time() - start_time,
    }
