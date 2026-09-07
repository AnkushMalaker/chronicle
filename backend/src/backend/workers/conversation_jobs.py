"""
Conversation-related RQ job functions.

This module contains jobs related to conversation management and updates.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

import openai
from omegaconf import OmegaConf
from rq import get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Job

from backend.config import get_live_segmentation, silence_trim_settings
from backend.config_loader import get_backend_config
from backend.constants import TITLE_NOT_GENERATED
from backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    enqueue_speech_detection,
    redis_conn,
    start_post_conversation_jobs,
    transcription_queue,
)
from backend.models.audio_chunk import AudioChunkDocument
from backend.models.conversation import Conversation, create_conversation
from backend.models.job import async_job
from backend.models.timeline import TimelineEpisode, utcnow
from backend.models.user import User
from backend.observability.otel_setup import (
    set_otel_session,
    set_span_attrs,
    set_trace_io,
    traced_job,
)
from backend.plugins.events import PluginEvent
from backend.services.audio_claims import (
    AudioClaimError,
    apply_audio_ranges,
    capture_clock_offset_for_ranges,
    claim_capture_window,
    clip_audio_ranges,
    merge_audio_ranges,
    resolve_conversation_audio,
)
from backend.services.audio_stream import TranscriptionResultsAggregator
from backend.services.audio_stream.conversation_lifecycle import (
    materialize_detected_conversation,
)
from backend.services.audio_stream.session_store import SessionStatus, SessionStore
from backend.services.device_context import request_conversation_context_jobs
from backend.services.memory import get_memory_service
from backend.services.plugin_service import (
    dispatch_or_defer_space_event,
    dispatch_plugin_event,
    get_plugin_router,
)
from backend.services.processing_artifacts import (
    persist_conversation_revision,
    persist_transcript_artifact,
)
from backend.services.sse_publisher import (
    publish_sse_event,
    publish_sse_event_throttled,
)
from backend.services.timeline.episode_summary import (
    bounded_episode_transcript,
    episode_summary_eligibility,
)
from backend.utils.audio_chunk_utils import invalidate_conversation_audio_caches
from backend.utils.audio_trim import (
    TrimPlan,
    plan_silence_trim,
    remap_segments,
    remap_words,
)
from backend.utils.conversation_utils import (
    analyze_speech,
    extract_speakers_from_segments,
    generate_conversation_title,
    generate_detailed_summary,
    generate_short_summary,
    is_meaningful_speech,
    mark_conversation_deleted,
    track_speech_activity,
    update_job_progress_metadata,
)
from backend.utils.job_utils import check_job_alive, update_job_meta
from backend.utils.transcript_slicing import build_transcript_text
from backend.utils.vad_analysis import analyze_conversation_audio

logger = logging.getLogger(__name__)


@async_job(redis=True, beanie=True)
@traced_job(
    "episode_detailed_summary",
    pipeline_stage="detailed_summary",
    gen_ai_operation="chat",
)
async def generate_episode_detailed_summary_job(
    episode_id: str,
    revision: int,
    scope_hash: str,
    dispatch_event_type: str,
    dispatch_claim_token: str,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """Generate the long account from all bounded sources of one episode revision."""

    # Lazy import: dispatch resolves this worker as its queue entry point.
    from backend.services.timeline.dispatch import (
        EPISODE_DETAILED_SUMMARY,
        release_episode_summary_claim,
    )

    try:
        expected_event_type = f"{EPISODE_DETAILED_SUMMARY}:{revision}:{scope_hash}"
        if dispatch_event_type != expected_event_type:
            raise ValueError("episode summary dispatch scope does not match the job")
        return await _generate_episode_detailed_summary(
            episode_id, revision, scope_hash
        )
    finally:
        try:
            await release_episode_summary_claim(
                dispatch_event_type,
                dispatch_claim_token,
            )
        except Exception:
            logger.exception(
                "Failed to release detailed-summary dispatch claim %s",
                dispatch_claim_token,
            )


async def _generate_episode_detailed_summary(
    episode_id: str, revision: int, scope_hash: str
) -> Dict[str, Any]:
    """Compute and conditionally materialize one exact summary scope."""

    episode = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode_id,
        TimelineEpisode.revision == revision,
    )
    if episode is None or episode.status == "superseded" or not episode.conversational:
        return {"success": True, "skipped": True, "episode_id": episode_id}
    eligibility = await episode_summary_eligibility(episode)
    if not eligibility.eligible or eligibility.scope_hash != scope_hash:
        return {
            "success": True,
            "skipped": True,
            "stale": True,
            "reason": eligibility.reason,
            "episode_id": episode_id,
        }
    if (
        episode.detailed_summary
        and episode.detailed_summary_scope_hash == scope_hash
        and episode.detailed_summary_revision == revision
    ):
        return {
            "success": True,
            "skipped": True,
            "already_materialized": True,
            "episode_id": episode_id,
            "revision": revision,
        }
    transcript = bounded_episode_transcript(episode)
    if not transcript:
        return {
            "success": False,
            "error": "No bounded transcript evidence available",
            "episode_id": episode_id,
        }
    detailed_summary = await generate_detailed_summary(transcript)
    current = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode_id,
        TimelineEpisode.revision == revision,
    )
    if current is None or current.status == "superseded":
        return {
            "success": True,
            "skipped": True,
            "stale": True,
            "episode_id": episode_id,
        }
    current_eligibility = await episode_summary_eligibility(current)
    if not current_eligibility.eligible or current_eligibility.scope_hash != scope_hash:
        return {
            "success": True,
            "skipped": True,
            "stale": True,
            "episode_id": episode_id,
        }
    generated_at = utcnow()
    updated = await TimelineEpisode.get_pymongo_collection().update_one(
        {
            "_id": current.id,
            "revision": revision,
            "status": {"$ne": "superseded"},
            "revised_at": current.revised_at,
            "$or": [
                {"detailed_summary": {"$in": [None, ""]}},
                {"detailed_summary_scope_hash": {"$ne": scope_hash}},
                {"detailed_summary_revision": {"$ne": revision}},
            ],
        },
        {
            "$set": {
                "detailed_summary": detailed_summary,
                "detailed_summary_scope_hash": scope_hash,
                "detailed_summary_revision": revision,
                "detailed_summary_generated_at": generated_at,
                "revised_at": generated_at,
            }
        },
    )
    if not updated.modified_count:
        winner = await TimelineEpisode.find_one(
            TimelineEpisode.episode_id == episode_id,
            TimelineEpisode.revision == revision,
        )
        if (
            winner is not None
            and winner.detailed_summary
            and winner.detailed_summary_scope_hash == scope_hash
            and winner.detailed_summary_revision == revision
        ):
            return {
                "success": True,
                "skipped": True,
                "already_materialized": True,
                "episode_id": episode_id,
                "revision": revision,
            }
        return {
            "success": True,
            "skipped": True,
            "stale": True,
            "episode_id": episode_id,
        }
    if updated.modified_count:
        landed = await TimelineEpisode.find_one(
            TimelineEpisode.episode_id == episode_id,
            TimelineEpisode.revision == revision,
        )
        landed_eligibility = (
            await episode_summary_eligibility(landed) if landed else None
        )
        if (
            landed_eligibility is None
            or not landed_eligibility.eligible
            or landed_eligibility.scope_hash != scope_hash
        ):
            await TimelineEpisode.get_pymongo_collection().update_one(
                {
                    "_id": current.id,
                    "detailed_summary": detailed_summary,
                    "detailed_summary_scope_hash": scope_hash,
                },
                {
                    "$unset": {
                        "detailed_summary": "",
                        "detailed_summary_scope_hash": "",
                        "detailed_summary_revision": "",
                        "detailed_summary_generated_at": "",
                    },
                    "$set": {"revised_at": utcnow()},
                },
            )
            return {
                "success": True,
                "skipped": True,
                "stale": True,
                "episode_id": episode_id,
            }
    if updated.modified_count:
        publish_sse_event(
            episode.user_id,
            "timeline.episode.updated",
            {"episode_id": episode_id, "revision": revision},
        )
    return {
        "success": bool(updated.matched_count),
        "episode_id": episode_id,
        "revision": revision,
        "detailed_summary": detailed_summary,
    }


async def trim_silence(
    conversation_id: str,
    speech_regions: Sequence[tuple[float, float]],
    *,
    pad_seconds: float = 5.0,
    min_run_seconds: float = 120.0,
    min_saving_seconds: float = 60.0,
    reason: str = "silence_trim",
) -> TrimPlan | None:
    """Trim the semantic claim while leaving every capture chunk untouched."""
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if conversation is None or conversation.deleted or not conversation.audio_ranges:
        return None
    resolved = await resolve_conversation_audio(conversation_id)
    chunks = [
        {
            "chunk_index": index,
            "start_time": item.conversation_start_seconds,
            "end_time": item.conversation_start_seconds + item.duration_seconds,
            "duration": item.duration_seconds,
        }
        for index, item in enumerate(resolved)
    ]

    plan = plan_silence_trim(
        chunks,
        speech_regions,
        pad_seconds=pad_seconds,
        min_run_seconds=min_run_seconds,
        min_saving_seconds=min_saving_seconds,
    )
    if not plan.trims:
        return None

    kept_groups = [
        await clip_audio_ranges(conversation.audio_ranges, old_start, old_end)
        for old_start, old_end, _new_start in plan.regions
    ]
    kept_ranges = merge_audio_ranges(kept_groups)

    # 3) Re-time every transcript version onto the trimmed timeline.
    #
    #    All of them, not just the active one: trimming moves the audio each version
    #    describes, so a version left behind keeps timings that outrun the audio. That
    #    stays invisible until something activates it — a rebuild resetting to the ASR
    #    layer, a manual version switch — and then speaker recognition fails with
    #    "end_time must be > start_time" on a segment past the end of the recording.
    for version in conversation.transcript_versions or []:
        version.segments = remap_segments(version.segments or [], plan.regions)
        version.words = remap_words(version.words or [], plan.regions)
        version.transcript = build_transcript_text(version.segments)

    await apply_audio_ranges(conversation, kept_ranges, save=False)
    conversation.vad_analysis = None

    # Transcript/diarization artifacts remain immutable evidence over the original
    # capture. The user-facing fusion is a derived projection, so trimming must write
    # a new standalone revision instead of leaving the active pointer on timings from
    # the pre-trim claim.
    active_version = conversation.active_transcript
    if active_version is not None:
        projection = {
            "operation": "silence_trim",
            "reason": reason,
            "regions": [list(region) for region in plan.regions],
            "audio_ranges": [
                audio_range.model_dump(mode="json") for audio_range in kept_ranges
            ],
        }
        active_version.metadata = dict(active_version.metadata)
        active_version.metadata["audio_projection"] = projection
        projection_digest = hashlib.sha256(
            json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        transcript_artifact_id = active_version.metadata.get("transcript_artifact_id")
        diarization_artifact_id = active_version.metadata.get("diarization_artifact_id")
        await persist_conversation_revision(
            conversation,
            active_version,
            retry_key=(
                f"silence-trim-projection:{conversation_id}:"
                f"{active_version.version_id}:{projection_digest}"
            ),
            transcript_artifact_ids=(
                [str(transcript_artifact_id)] if transcript_artifact_id else []
            ),
            diarization_artifact_ids=(
                [str(diarization_artifact_id)] if diarization_artifact_id else []
            ),
        )
    await conversation.save()
    await invalidate_conversation_audio_caches(conversation_id)

    logger.info(
        f"✂️ Removed {plan.dropped_seconds:.0f}s of long silence from semantic claim "
        f"{conversation_id[:12]}; capture chunks remain unchanged"
    )
    return plan


async def maybe_trim_silence(conversation_id: str) -> TrimPlan | None:
    """Trim a finalized conversation's silence, best-effort.

    Continuous capture records regardless of whether anything is happening, so its
    semantic claims may include long leading or interior silence.

    Speech regions come from VAD over the audio (``analyze_conversation_audio``) rather
    than from transcript word timings: the streaming transcript times words relative to
    the speech onset (word[0].start == 0.0), so it cannot locate where speech sits in
    the session timeline — only the batch path produces absolute times. VAD is correct
    for both finalization paths, and it populates the per-chunk ``vad`` field as a side
    benefit.

    Never raises: trimming is an optimization and must not block finalize.
    """
    try:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if conversation is None or conversation.deleted:
            return None

        settings = silence_trim_settings()
        if not settings.get("enabled", True):
            return None
        min_run = float(settings.get("min_run_seconds", 120.0))

        # A conversation shorter than one cuttable run cannot have one — skip the
        # VAD decode entirely.
        total_duration = float(
            getattr(conversation, "audio_total_duration", 0.0) or 0.0
        )
        if total_duration < min_run:
            return None

        analysis = await analyze_conversation_audio(conversation_id)
        regions = [
            (float(start), float(end))
            for start, end in (analysis.get("speech_regions") or [])
        ]
        if not regions:
            # No speech at all. Trimming would empty the conversation; whether it
            # should exist is the speech gate's decision, not this one's.
            return None

        return await trim_silence(
            conversation_id,
            regions,
            pad_seconds=float(settings.get("pad_seconds", 5.0)),
            min_run_seconds=min_run,
            min_saving_seconds=float(settings.get("min_saving_seconds", 60.0)),
        )
    except Exception as e:  # noqa: BLE001 — trimming must never block finalize
        logger.warning(f"Silence trim skipped for {conversation_id[:12]}: {e}")
        return None


async def _claim_persisted_capture(
    conversation: Conversation,
    capture_session_id: str,
    ended_at: datetime,
    *,
    max_wait_seconds: float = 30.0,
) -> bool:
    """Wait for persistence to reach the semantic end, then attach its claim."""
    deadline = time.time() + max_wait_seconds
    latest = None
    while time.time() < deadline:
        latest = (
            await AudioChunkDocument.find(
                AudioChunkDocument.capture_session_id == capture_session_id,
                AudioChunkDocument.deleted == False,  # noqa: E712 - Beanie expression
            )
            .sort("-sequence")
            .first_or_none()
        )
        if latest is not None:
            latest_end = latest.captured_at + timedelta(seconds=latest.duration)
            if latest_end.tzinfo is None:
                latest_end = latest_end.replace(tzinfo=timezone.utc)
            if latest_end >= ended_at - timedelta(milliseconds=250):
                break
        await asyncio.sleep(0.5)
    if latest is None:
        return False

    latest_end = latest.captured_at + timedelta(seconds=latest.duration)
    if latest_end.tzinfo is None:
        latest_end = latest_end.replace(tzinfo=timezone.utc)
    audio_ranges = await claim_capture_window(
        capture_session_id,
        conversation.started_at,
        min(ended_at, latest_end),
    )
    await apply_audio_ranges(conversation, audio_ranges)
    return True


def should_discard_unbacked_conversation(has_meaningful_transcript: bool) -> bool:
    """Decide whether a conversation with no persisted audio should be discarded.

    A conversation whose capture chunks never landed is discarded only if it carries
    no meaningful transcript.
    Losing a real transcript is worse than keeping an audio-less conversation, so a
    transcript-bearing conversation is salvaged, not deleted.
    """
    return not has_meaningful_transcript


async def handle_end_of_conversation(
    session_id: str,
    conversation_id: str,
    client_id: str,
    user_id: str,
    start_time: float,
    last_result_count: int,
    timeout_triggered: bool,
    redis_client,
    end_reason: str = "unknown",
) -> Dict[str, Any]:
    """
    Handle end-of-conversation cleanup and session restart logic.

    This function is called at the end of open_conversation_job to:
    1. Clean up Redis streams and tracking keys
    2. Increment conversation count for the session
    3. Re-enqueue speech detection job if session is still active
    4. Record conversation end reason in database

    Args:
        session_id: Stream session ID
        conversation_id: Conversation ID that just completed
        client_id: Client ID
        user_id: User ID
        start_time: Job start time (for runtime calculation)
        last_result_count: Number of transcription results processed
        timeout_triggered: Whether closure was due to inactivity timeout
        redis_client: Redis client instance
        end_reason: Reason conversation ended (user_stopped, inactivity_timeout, websocket_disconnect, etc.)

    Returns:
        Dict with conversation_id, conversation_count, final_result_count, runtime_seconds, timeout_triggered, end_reason
    """
    store = SessionStore(redis_client)
    # Clean up Redis streams to prevent memory leaks
    try:
        # Do NOT delete audio:stream:{session_id} here. It is the immutable WAL for
        # the whole recording and remains active across conversation rotations.
        # Only consumer-group drain proof may delete it after producer finalization.

        # Delete the transcription results stream (per-session/conversation)
        results_stream_key = f"transcription:results:{session_id}"
        await redis_client.delete(results_stream_key)
        logger.info(f"🧹 Deleted results stream: {results_stream_key}")

        # NOTE: session-hash TTL is handled below, gated on whether the session is
        # ending vs. continuing. Setting it here (unconditionally) would let the hash
        # expire mid-session for a still-active connection → zombie session.
    except Exception as cleanup_error:
        logger.warning(f"⚠️ Error during stream cleanup: {cleanup_error}")

    # Update conversation in database with end reason and completion time
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if conversation:
        # Convert string to enum
        try:
            conversation.end_reason = Conversation.EndReason(end_reason)
        except ValueError:
            logger.warning(f"⚠️ Invalid end_reason '{end_reason}', using UNKNOWN")
            conversation.end_reason = Conversation.EndReason.UNKNOWN

        conversation.completed_at = datetime.now(timezone.utc)
        await conversation.save()
        logger.info(
            f"💾 Saved conversation {conversation_id[:12]} end_reason: {conversation.end_reason}"
        )
    else:
        logger.warning(
            f"⚠️ Conversation {conversation_id} not found for end reason tracking"
        )

    await store.clear_active_conversation(session_id, expected_id=conversation_id)

    # Increment conversation count for this session
    conversation_count = await store.increment_conversation_count(session_id)
    logger.info(f"📊 Conversation count for session {session_id}: {conversation_count}")

    # Check if session is still active (user still recording) and restart listening jobs
    # Fetch status, websocket_connected, and completion_reason in one Redis call
    status, ws_connected, completion_reason = await store.get_status_ws_reason(
        session_id
    )

    if status is not None:
        # Determine if we should restart speech detection
        # Only restart when session is explicitly active.
        # status=finalizing means the session is ending (audio-stop or disconnect),
        # so re-enqueueing speech detection for the same session is always wrong.
        should_restart = False
        if status == SessionStatus.ACTIVE:
            should_restart = True
        else:
            logger.info(
                f"Session {session_id[:12]}: status={status.value}, "
                f"ws_connected={ws_connected}, completion_reason={completion_reason} "
                f"— not restarting speech detection."
            )

        if should_restart:
            # Session still active - enqueue new speech detection for next conversation.
            # Clear any TTL: the hash must live as long as the connection does, otherwise
            # a quiet gap > TTL expires it mid-session → next close reads status=None →
            # speech detection never restarts, leaving the connection unable to receive audio.
            await store.persist_session(session_id)
            logger.info(
                f"🔄 Enqueueing new speech detection (conversation #{conversation_count + 1})"
            )

            # Clear transcription completion flag so streaming consumer can re-attach
            # (if it exited during previous conversation, this flag prevents re-discovery)
            completion_key = f"transcription:complete:{session_id}"
            await redis_client.delete(completion_key)
            logger.info(f"🧹 Cleared transcription completion flag: {completion_key}")

            # Enqueue speech detection for the next conversation (audio persistence
            # keeps running). Single-flight: when several conversation-end handlers
            # fire together for the same session (the old duplicate-job storm), only
            # one detector is created instead of one per handler.
            enqueue_speech_detection(
                session_id, user_id, client_id, reason="conversation_end"
            )
        else:
            # Session ending (finalizing/finished): set a backstop TTL so the hash
            # self-cleans if the disconnect path didn't already remove it.
            await store.expire_session(session_id, 3600)
            status_label = status.value if status is not None else "missing"
            logger.info(
                f"Session {session_id} status={status_label}, ws_connected={ws_connected}, "
                f"not restarting (user stopped recording) — set 1h cleanup TTL"
            )
    else:
        logger.info(f"Session {session_id} not found, not restarting (session ended)")

    # Notify frontend that conversation has closed and post-processing will start
    publish_sse_event(
        user_id,
        "conversation.closed",
        {
            "conversation_id": conversation_id,
            "client_id": client_id,
            "end_reason": end_reason,
        },
    )

    return {
        "conversation_id": conversation_id,
        "conversation_count": conversation_count,
        "deleted": False,  # This conversation was not deleted (normal completion)
        "final_result_count": last_result_count,
        "runtime_seconds": time.time() - start_time,
        "timeout_triggered": timeout_triggered,
        "end_reason": end_reason,
    }


@dataclass
class ConversationState:
    """Mutable state tracked across the conversation monitoring loop."""

    conversation_id: str = ""
    session_id: str = ""
    user_id: str = ""
    client_id: str = ""
    memory_space_id: str = ""
    start_time: float = 0.0
    last_result_count: int = 0
    timeout_triggered: bool = False
    close_requested_reason: Optional[str] = None
    last_meaningful_speech_time: float = 0.0
    last_word_count: int = 0
    end_reason: str = "unknown"
    live_version_created: bool = False
    last_live_write_time: float = 0.0
    capture_clock_offset_seconds: Optional[float] = None
    last_live_clock_warning_time: float = 0.0


@dataclass
class LiveTranscriptState:
    """Incremental accumulation of transcription results.

    Instead of re-reading the entire Redis Stream via XRANGE every second,
    this tracks position in the stream and accumulates only new results.
    """

    text_parts: list
    all_words: list
    all_segments: list
    chunk_count: int = 0
    total_confidence: float = 0.0
    provider: Optional[str] = None
    last_stream_id: str = "0"

    def add_results(self, new_results: list) -> None:
        """Incrementally add new transcription results."""
        for result in new_results:
            text = result.get("text", "").strip()
            if text:
                self.text_parts.append(text)
            self.all_words.extend(result.get("words", []))
            self.all_segments.extend(result.get("segments", []))
            self.total_confidence += result.get("confidence", 0.0)
            self.chunk_count += 1
            if self.provider is None:
                self.provider = result.get("provider")

    def to_combined(self) -> dict:
        """Return dict matching ``get_combined_results()`` format."""
        return {
            "text": " ".join(self.text_parts),
            "words": self.all_words,
            "segments": self.all_segments,
            "chunk_count": self.chunk_count,
            "total_confidence": (
                self.total_confidence / self.chunk_count
                if self.chunk_count > 0
                else 0.0
            ),
            "provider": self.provider,
            "word_count": len(self.all_words),
        }


async def _wait_for_new_results(
    redis_client, session_id: str, last_stream_id: str
) -> tuple:
    """Block until new transcription results arrive on the Redis Stream.

    Uses XREAD with blocking to avoid polling. Returns only NEW messages
    since ``last_stream_id``.

    Returns:
        (parsed_results_list, new_last_id)
    """
    stream_name = f"transcription:results:{session_id}"
    try:
        messages = await redis_client.xread(
            {stream_name: last_stream_id}, count=100, block=10000
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Error in _wait_for_new_results: {e}")
        return [], last_stream_id

    if not messages:
        return [], last_stream_id

    results = []
    new_last_id = last_stream_id
    for _stream_key, msgs in messages:
        for message_id, fields in msgs:
            mid = message_id if isinstance(message_id, str) else message_id.decode()
            result = {
                "message_id": mid,
                "text": (fields[b"text"].decode() if b"text" in fields else ""),
                "confidence": float(
                    fields.get(b"confidence", b"0.0").decode()
                    if isinstance(fields.get(b"confidence", b"0.0"), bytes)
                    else fields.get(b"confidence", 0.0)
                ),
                "provider": (
                    fields[b"provider"].decode() if b"provider" in fields else "unknown"
                ),
                "chunk_id": (
                    fields.get(b"chunk_id", b"unknown").decode()
                    if isinstance(fields.get(b"chunk_id", b"unknown"), bytes)
                    else str(fields.get(b"chunk_id", "unknown"))
                ),
                "timestamp": float(
                    fields.get(b"timestamp", b"0.0").decode()
                    if isinstance(fields.get(b"timestamp", b"0.0"), bytes)
                    else fields.get(b"timestamp", 0.0)
                ),
            }
            if b"words" in fields:
                result["words"] = json.loads(
                    fields[b"words"].decode()
                    if isinstance(fields[b"words"], bytes)
                    else fields[b"words"]
                )
            if b"segments" in fields:
                result["segments"] = json.loads(
                    fields[b"segments"].decode()
                    if isinstance(fields[b"segments"], bytes)
                    else fields[b"segments"]
                )
            results.append(result)
            new_last_id = mid

    return results, new_last_id


async def _wait_for_signal(pubsub) -> Optional[dict]:
    """Block until a session signal arrives via Redis Pub/Sub.

    Returns parsed JSON signal dict, or None on timeout.
    """
    try:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=10.0)
        if message and message["type"] == "message":
            data = message["data"]
            if isinstance(data, bytes):
                data = data.decode()
            return json.loads(data)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Error in _wait_for_signal: {e}")
    return None


def _validate_segments(segments: list) -> list:
    """Validate and filter transcription segments, correcting minor issues.

    Filters out non-dict segments and segments with no text. Corrects invalid
    timing (end <= start) by estimating duration from word count. Ensures
    speaker field is always a non-empty string.
    """
    validated = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            logger.warning(f"Segment {i} is not a dict: {type(seg)}")
            continue

        text = seg.get("text", "").strip()
        if not text:
            logger.debug(f"Segment {i} has no text, skipping")
            continue

        start = seg.get("start", 0.0)
        end = seg.get("end", 0.0)
        if end <= start:
            logger.debug(
                f"Segment {i} has invalid timing (start={start}, end={end}), correcting"
            )
            estimated_duration = len(text.split()) * 0.5  # ~0.5 seconds per word
            seg["end"] = start + estimated_duration

        speaker = seg.get("speaker")
        if speaker is None or speaker == "":
            seg["speaker"] = "SPEAKER_00"
        elif isinstance(speaker, (int, float)):
            seg["speaker"] = f"Speaker {int(speaker)}"

        validated.append(seg)

    logger.info(f"Validated {len(validated)}/{len(segments)} segments")
    return validated


async def _initialize_conversation(
    session_id: str,
    user_id: str,
    client_id: str,
    speech_job_id: str,
    speech_detected_at: float,
    current_job,
    redis_client,
) -> str:
    """Materialize a detected Conversation without affecting capture persistence.

    Returns:
        conversation_id of the created/reused conversation.
    """
    store = SessionStore(redis_client)
    materialized = await materialize_detected_conversation(
        capture_session_id=session_id,
        user_id=user_id,
        client_id=client_id,
        speech_detected_at=speech_detected_at,
    )
    conversation = materialized.conversation
    conversation_id = conversation.conversation_id
    if not await store.set_active_conversation(session_id, conversation_id):
        active_id = await store.get_active_conversation_id(session_id)
        session_view = await store.read(session_id)
        terminal_after_final_result = bool(
            session_view is not None
            and session_view.status
            in (SessionStatus.FINALIZING, SessionStatus.FINISHED)
            and active_id in (None, conversation_id)
        )
        if not terminal_after_final_result:
            raise RuntimeError(
                f"Session {session_id} cannot activate conversation {conversation_id}; "
                f"status is not active or {active_id!r} is already open"
            )
        logger.info(
            "Session %s became %s before final speech materialization; "
            "continuing without an active Conversation pointer",
            session_id,
            session_view.status.value,
        )

    if materialized.created:
        logger.info(
            f"✅ Created streaming conversation {conversation_id} for session {session_id}"
        )
        publish_sse_event(
            user_id,
            "conversation.created",
            {
                "conversation_id": conversation_id,
                "client_id": client_id,
                "title": TITLE_NOT_GENERATED,
            },
        )

    # Attach markers from Redis session (e.g., button events captured during streaming)
    markers = await store.get_markers(session_id)
    if markers:
        conversation.markers = markers
        await conversation.save()
        logger.info(
            f"📌 Attached {len(conversation.markers)} markers to conversation {conversation_id}"
        )

    # Link job metadata to conversation (cascading updates)
    current_job.meta["conversation_id"] = conversation_id
    current_job.save_meta()

    try:
        speech_job = Job.fetch(speech_job_id, connection=redis_conn)
        speech_job.meta["conversation_id"] = conversation_id
        speech_job.save_meta()
        speaker_check_job_id = speech_job.meta.get("speaker_check_job_id")
        if speaker_check_job_id:
            try:
                speaker_check_job = Job.fetch(
                    speaker_check_job_id, connection=redis_conn
                )
                speaker_check_job.meta["conversation_id"] = conversation_id
                speaker_check_job.save_meta()
            except Exception as e:
                if isinstance(e, NoSuchJobError):
                    logger.error(
                        f"❌ Missing job hash for speaker_check job {speaker_check_job_id}: "
                        f"Job was linked to speech_job {speech_job_id} but hash key disappeared. "
                        f"This may indicate TTL expiry or job collision."
                    )
                else:
                    raise
    except Exception as e:
        if isinstance(e, NoSuchJobError):
            logger.error(
                f"❌ Missing job hash for speech_job {speech_job_id}: "
                f"Job was created for session {session_id} but hash key disappeared before metadata link. "
                f"This may indicate TTL expiry or job collision."
            )
        else:
            raise

    return conversation_id


async def _monitor_conversation_loop(
    state: ConversationState,
    aggregator,
    current_job,
    redis_client,
) -> None:
    """Monitor transcription results and session signals until conversation ends.

    Event-driven architecture using ``asyncio.wait()`` with:
    - Redis ``XREAD`` blocking for new transcription results (no full-stream re-read)
    - Redis Pub/Sub for session signals (finalize, close_requested)
    - Periodic timeout for housekeeping (zombie detection, inactivity check)

    Mutates ``state`` in place with final values for timeout_triggered,
    close_requested_reason, last_result_count, and last_word_count.
    """
    store = SessionStore(redis_client)
    max_runtime = 86400  # 24h safety ceiling

    # Inactivity timeout configuration
    inactivity_timeout_seconds = float(
        os.getenv("SPEECH_INACTIVITY_THRESHOLD_SECONDS", "60")
    )
    inactivity_timeout_minutes = inactivity_timeout_seconds / 60
    last_inactivity_log_time = time.time()

    # Audio-stream idle watchdog (wall-clock). The producer bumps last_chunk_at on
    # every audio chunk, so it advances continuously while the device streams — even
    # during silence — and freezes the instant the device disconnects. The
    # audio-timestamp-based inactivity check below cannot grow without new results,
    # so on a sudden disconnect it would hang until the 24h ceiling. This watchdog
    # is the reliable "device stopped streaming" signal, independent of WebSocket
    # cleanup running. It only applies when live transcription is enabled — in
    # "off"/batch mode no streaming worker fills the aggregator and chunks may stop
    # without the conversation being stuck (the batch path closes it).
    expects_live_results = get_live_segmentation() != "off"
    stream_idle_timeout_seconds = float(os.getenv("STREAM_IDLE_TIMEOUT_SECONDS", "30"))
    health_check_interval = 5.0  # wall-clock cadence for status + chunk-idle polling
    last_health_check_time = time.time()

    # Test mode: wait for audio queue to drain before timing out
    wait_for_queue_drain = (
        os.getenv("WAIT_FOR_AUDIO_QUEUE_DRAIN", "false").lower() == "true"
    )

    logger.info(
        f"📊 Conversation timeout configured: {inactivity_timeout_minutes} minutes ({inactivity_timeout_seconds}s)"
    )
    if wait_for_queue_drain:
        logger.info("🧪 Test mode: Waiting for audio queue to drain before timeout")

    # Subscribe to session signals via Pub/Sub
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"session:signal:{state.session_id}")

    # Incremental result accumulation (replaces full XRANGE every second)
    transcript_state = LiveTranscriptState(text_parts=[], all_words=[], all_segments=[])

    # Track inactivity based on accumulated speech data
    inactivity_duration = 0.0

    try:
        while True:
            # Create concurrent event sources
            results_task = asyncio.create_task(
                _wait_for_new_results(
                    redis_client,
                    state.session_id,
                    transcript_state.last_stream_id,
                )
            )
            signal_task = asyncio.create_task(_wait_for_signal(pubsub))

            # Wait for EITHER new results, a signal, or timeout (housekeeping)
            done, pending = await asyncio.wait(
                {results_task, signal_task},
                timeout=10.0,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            # --- Handle signal ---
            if signal_task in done:
                signal = signal_task.result()
                if signal:
                    sig_type = signal.get("type")
                    sig_reason = signal.get("reason", "unknown")

                    if sig_type == "finalize":
                        if sig_reason == "websocket_disconnect":
                            logger.warning(
                                f"🔌 WebSocket disconnected for session {state.session_id[:12]} - "
                                f"ending conversation early"
                            )
                        else:
                            logger.info(f"🛑 Session finalizing (reason: {sig_reason})")
                        break

                    elif sig_type == "close_requested":
                        await store.take_close_request(state.session_id)
                        state.close_requested_reason = sig_reason
                        logger.info(f"🔒 Conversation close requested: {sig_reason}")
                        state.timeout_triggered = True
                        break

                    elif sig_type == "session_complete":
                        # Check for spurious "finished" from status endpoint race
                        if sig_reason == "all_jobs_complete":
                            if await store.is_websocket_connected(state.session_id):
                                logger.warning(
                                    f"⚠️ Ignoring spurious 'finished' for session {state.session_id[:12]}: "
                                    f"websocket_connected=true, reason=all_jobs_complete. "
                                    f"Resetting status to 'active'."
                                )
                                await store.set_status_active(state.session_id)
                                # Continue monitoring
                            else:
                                break
                        else:
                            break

            # --- Handle new transcription results ---
            if results_task in done:
                new_results, new_last_id = results_task.result()
                if new_results:
                    transcript_state.add_results(new_results)
                    transcript_state.last_stream_id = new_last_id
                    combined = transcript_state.to_combined()
                    current_count = combined["chunk_count"]

                    # Analyze speech content
                    transcript_data = {
                        "text": combined["text"],
                        "words": combined.get("words", []),
                    }
                    speech_analysis = analyze_speech(transcript_data)

                    # Validate segments and extract speakers
                    validated_segments = _validate_segments(
                        combined.get("segments", [])
                    )
                    speakers = extract_speakers_from_segments(validated_segments)

                    # Track speech activity
                    new_speech_time, state.last_word_count = (
                        await track_speech_activity(
                            speech_analysis=speech_analysis,
                            last_word_count=state.last_word_count,
                            conversation_id=state.conversation_id,
                            redis_client=redis_client,
                        )
                    )
                    if new_speech_time:
                        state.last_meaningful_speech_time = new_speech_time

                    # Update job metadata
                    await update_job_progress_metadata(
                        current_job=current_job,
                        conversation_id=state.conversation_id,
                        session_id=state.session_id,
                        client_id=state.client_id,
                        combined=combined,
                        speech_analysis=speech_analysis,
                        speakers=speakers,
                        last_meaningful_speech_time=state.last_meaningful_speech_time,
                    )

                    # Push live progress via SSE
                    publish_sse_event_throttled(
                        state.user_id,
                        "job.progress",
                        {
                            "conversation_id": state.conversation_id,
                            "job_type": "open_conversation_job",
                            "word_count": combined.get("word_count", 0),
                            "duration_seconds": speech_analysis.get("duration", 0),
                            "speakers": speakers,
                            "has_speech": speech_analysis.get("has_speech", False),
                        },
                    )

                    # Update inactivity tracking
                    current_audio_time = speech_analysis.get("speech_end", 0.0)
                    if current_audio_time > 0 and state.last_meaningful_speech_time > 0:
                        inactivity_duration = (
                            current_audio_time - state.last_meaningful_speech_time
                        )
                    else:
                        inactivity_duration = 0

                    # Track results progress
                    if current_count > state.last_result_count:
                        logger.info(
                            f"📊 Conversation {state.conversation_id} progress: "
                            f"{current_count} results, {len(combined['text'])} chars, "
                            f"{len(validated_segments)} segments"
                        )
                        state.last_result_count = current_count

                        # Live consumers use the Conversation audio clock too.  Do
                        # not publish raw capture-session timestamps while waiting
                        # for persistence to prove the exact sample-clock origin.
                        now_live = time.time()
                        live_segments = None
                        live_clock_metadata = None
                        try:
                            capture_clock_offset_seconds = (
                                await _resolve_live_capture_clock_offset(state)
                            )
                            live_segments = deepcopy(validated_segments)
                            _rebase_timestamps_to_conversation_start(
                                [],
                                live_segments,
                                capture_clock_offset_seconds=(
                                    capture_clock_offset_seconds
                                ),
                            )
                            live_clock_metadata = _streaming_clock_metadata(
                                state.session_id,
                                timestamp_clock="conversation",
                                timestamp_rebase_seconds=(capture_clock_offset_seconds),
                            )
                        # Timed live output must fail closed when the persisted
                        # capture prefix cannot prove the clock transform.
                        except Exception as e:  # noqa: BLE001
                            if now_live - state.last_live_clock_warning_time >= 30.0:
                                logger.warning(
                                    "Live transcript clock unresolved for %s; "
                                    "withholding timed Mongo/SSE/plugin projection: %s",
                                    state.conversation_id[:12],
                                    e,
                                )
                                state.last_live_clock_warning_time = now_live

                        # Update live transcript in MongoDB (throttled to every 5s).
                        if (
                            live_segments is not None
                            and live_clock_metadata is not None
                            and not state.memory_space_id
                        ):
                            try:
                                capture_clock_offset_seconds = float(
                                    live_clock_metadata["timestamp_rebase_seconds"]
                                )
                                provider = combined.get("provider") or "unknown"
                                if not state.live_version_created:
                                    await _create_live_transcript_version(
                                        conversation_id=state.conversation_id,
                                        combined=combined,
                                        validated_segments=live_segments,
                                        provider=provider,
                                        capture_session_id=state.session_id,
                                        capture_clock_offset_seconds=(
                                            capture_clock_offset_seconds
                                        ),
                                    )
                                    state.live_version_created = True
                                    state.last_live_write_time = now_live
                                    publish_sse_event(
                                        state.user_id,
                                        "transcript.live",
                                        {
                                            "conversation_id": state.conversation_id,
                                            "segments": live_segments,
                                            "transcript": combined.get("text", ""),
                                            "word_count": combined.get("word_count", 0),
                                            **live_clock_metadata,
                                        },
                                    )
                                elif now_live - state.last_live_write_time >= 5.0:
                                    await _update_live_transcript(
                                        conversation_id=state.conversation_id,
                                        combined=combined,
                                        validated_segments=live_segments,
                                        capture_session_id=state.session_id,
                                        capture_clock_offset_seconds=(
                                            capture_clock_offset_seconds
                                        ),
                                    )
                                    state.last_live_write_time = now_live
                                    publish_sse_event(
                                        state.user_id,
                                        "transcript.live",
                                        {
                                            "conversation_id": state.conversation_id,
                                            "segments": live_segments,
                                            "transcript": combined.get("text", ""),
                                            "word_count": combined.get("word_count", 0),
                                            **live_clock_metadata,
                                        },
                                    )
                            except Exception as e:
                                logger.warning(
                                    f"⚠️ Error updating live transcript: {e}"
                                )

                        # Dispatch transcript.streaming plugin events only after the
                        # same conversation-clock projection succeeds.
                        if (
                            live_segments is not None
                            and live_clock_metadata is not None
                        ):
                            try:
                                plugin_router = get_plugin_router()
                                if plugin_router:
                                    transcript_text = combined.get("text", "")
                                    if transcript_text:
                                        plugin_data = {
                                            "transcript": transcript_text,
                                            "segment_id": f"{state.session_id}_{current_count}",
                                            "conversation_id": state.conversation_id,
                                            "segments": live_segments,
                                            "word_count": speech_analysis.get(
                                                "word_count", 0
                                            ),
                                            **live_clock_metadata,
                                        }
                                        logger.info(
                                            f"🔌 DISPATCH: transcript.streaming event "
                                            f"(conversation={state.conversation_id[:12]}, "
                                            f"segment_id={state.session_id}_{current_count})"
                                        )
                                        plugin_results = (
                                            await plugin_router.dispatch_event(
                                                event=PluginEvent.TRANSCRIPT_STREAMING,
                                                user_id=state.user_id,
                                                data=plugin_data,
                                                metadata={"client_id": state.client_id},
                                            )
                                        )
                                        logger.info(
                                            f"🔌 RESULT: transcript.streaming dispatched to "
                                            f"{len(plugin_results) if plugin_results else 0} plugins"
                                        )
                                        if plugin_results:
                                            for result in plugin_results:
                                                if result.message:
                                                    logger.info(
                                                        f"  Plugin: {result.message}"
                                                    )
                                                if not result.should_continue:
                                                    logger.info(
                                                        f"  Plugin stopped normal processing"
                                                    )
                            except Exception as e:
                                logger.warning(
                                    f"⚠️ Error triggering transcript-level plugins: {e}"
                                )

            # --- Housekeeping (runs on timeout or after processing) ---
            current_time = time.time()

            # Zombie detection
            if not await check_job_alive(redis_client, current_job, state.session_id):
                break

            # Max runtime
            if current_time - state.start_time > max_runtime:
                logger.warning(f"⏱️ Max runtime reached for {state.conversation_id}")
                break

            # Periodic health check (status + audio-stream liveness). Runs on a
            # wall-clock cadence regardless of whether results/signals fired this
            # iteration — so a missed pub/sub finalize signal or a silently
            # disconnected device is still detected, not just when the loop happens
            # to time out idle.
            if current_time - last_health_check_time >= health_check_interval:
                last_health_check_time = current_time

                status, ws_connected, reason_str = await store.get_status_ws_reason(
                    state.session_id
                )
                reason_str = reason_str or "unknown"
                if status in (SessionStatus.FINALIZING, SessionStatus.FINISHED):
                    if (
                        status == SessionStatus.FINISHED
                        and ws_connected
                        and reason_str == "all_jobs_complete"
                    ):
                        await store.set_status_active(state.session_id)
                    else:
                        logger.info(
                            f"🔍 Health check: session {state.session_id[:12]} "
                            f"status={status.value}, reason={reason_str}"
                        )
                        if reason_str == "websocket_disconnect":
                            state.timeout_triggered = False
                        break

                # Catch a missed close_requested signal
                close_reason = await store.take_close_request(state.session_id)
                if close_reason:
                    state.close_requested_reason = close_reason
                    logger.info(
                        f"🔍 Health check: conversation close requested: {state.close_requested_reason}"
                    )
                    state.timeout_triggered = True
                    break

                # Audio-stream idle watchdog: no audio chunks for too long means the
                # device stopped streaming (sudden disconnect) even though status may
                # still read ACTIVE (WebSocket cleanup not yet run). Skipped in
                # "off"/batch mode, where no streaming worker produces chunks here.
                if expects_live_results:
                    last_chunk_at = await store.get_last_chunk_at(state.session_id)
                    if (
                        last_chunk_at
                        and current_time - last_chunk_at > stream_idle_timeout_seconds
                    ):
                        logger.warning(
                            f"🔌 No audio chunks for {current_time - last_chunk_at:.0f}s "
                            f"(threshold {stream_idle_timeout_seconds:.0f}s) — device "
                            f"stopped streaming, closing conversation "
                            f"{state.conversation_id[:12]}"
                        )
                        state.timeout_triggered = True
                        break

            # Inactivity log and check
            if current_time - last_inactivity_log_time >= 10:
                logger.info(
                    f"⏱️ Time since last speech: {inactivity_duration:.1f}s "
                    f"(timeout: {inactivity_timeout_seconds:.0f}s)"
                )
                last_inactivity_log_time = current_time

            if inactivity_duration > inactivity_timeout_seconds:
                if wait_for_queue_drain:
                    persist_queue_key = f"audio:queue:{state.session_id}"
                    queue_length = await redis_client.llen(persist_queue_key)
                    if queue_length > 0:
                        logger.info(
                            f"🧪 Test mode: Inactivity timeout reached but "
                            f"{queue_length} chunks still in queue, waiting..."
                        )
                        continue

                logger.info(
                    f"🕐 Conversation {state.conversation_id} inactive for "
                    f"{inactivity_duration / 60:.1f} minutes "
                    f"(threshold: {inactivity_timeout_minutes} min), "
                    f"auto-closing conversation..."
                )
                state.timeout_triggered = True
                break

    finally:
        await pubsub.unsubscribe(f"session:signal:{state.session_id}")
        await pubsub.aclose()


async def _create_live_transcript_version(
    conversation_id: str,
    combined: dict,
    validated_segments: list,
    provider: str,
    *,
    capture_session_id: str,
    capture_clock_offset_seconds: float,
) -> None:
    """Create the initial live-v0 transcript version via Beanie update.

    Uses a single atomic update to push the version and set it as active,
    avoiding a full Beanie document load/save cycle.
    """
    transcript_text = combined.get("text", "")
    segments_as_dicts = [
        {
            "start": s.get("start", 0.0),
            "end": s.get("end", 0.0),
            "text": s.get("text", ""),
            "speaker": str(s.get("speaker", "SPEAKER_00")),
            "segment_type": s.get("segment_type", "speech"),
            "identified_as": s.get("identified_as"),
            "confidence": s.get("confidence"),
            "words": [],
        }
        for s in validated_segments
    ]

    version_doc = {
        "version_id": "live-v0",
        "transcript": transcript_text,
        "words": [],
        "segments": segments_as_dicts,
        "provider": provider,
        "model": provider,
        "created_at": datetime.now(timezone.utc),
        "processing_time_seconds": None,
        "diarization_source": "provider" if segments_as_dicts else None,
        "metadata": {
            "source": "live_streaming",
            "word_count": combined.get("word_count", 0),
            **_streaming_clock_metadata(
                capture_session_id,
                timestamp_clock="conversation",
                timestamp_rebase_seconds=capture_clock_offset_seconds,
            ),
        },
    }

    result = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    ).update(
        {
            "$push": {"transcript_versions": version_doc},
            "$set": {"active_transcript_version": "live-v0"},
        }
    )

    logger.info(
        f"📡 Created live-v0 transcript version for {conversation_id[:12]} "
        f"({len(transcript_text)} chars, {len(segments_as_dicts)} segments)"
    )


async def _update_live_transcript(
    conversation_id: str,
    combined: dict,
    validated_segments: list,
    *,
    capture_session_id: str,
    capture_clock_offset_seconds: float,
) -> None:
    """Update the live-v0 transcript version in-place via positional $ operator.

    Efficient partial update — only touches the transcript text, segments, and
    metadata.word_count within the matching array element.
    Uses PyMongo collection (sync) wrapped in Beanie's find pattern.
    """
    transcript_text = combined.get("text", "")
    segments_as_dicts = [
        {
            "start": s.get("start", 0.0),
            "end": s.get("end", 0.0),
            "text": s.get("text", ""),
            "speaker": str(s.get("speaker", "SPEAKER_00")),
            "segment_type": s.get("segment_type", "speech"),
            "identified_as": s.get("identified_as"),
            "confidence": s.get("confidence"),
            "words": [],
        }
        for s in validated_segments
    ]

    # Use PyMongo collection for positional $ operator (Beanie doesn't support it)
    collection = Conversation.get_pymongo_collection()
    collection.update_one(
        {
            "conversation_id": conversation_id,
            "transcript_versions.version_id": "live-v0",
        },
        {
            "$set": {
                "transcript_versions.$.transcript": transcript_text,
                "transcript_versions.$.segments": segments_as_dicts,
                "transcript_versions.$.metadata.word_count": combined.get(
                    "word_count", 0
                ),
                "transcript_versions.$.metadata.source_timestamp_clock": (
                    "capture_session"
                ),
                "transcript_versions.$.metadata.timestamp_clock": "conversation",
                "transcript_versions.$.metadata.timestamp_rebase_seconds": (
                    capture_clock_offset_seconds
                ),
                "transcript_versions.$.metadata.capture_session_id": (
                    capture_session_id
                ),
            }
        },
    )


def _rebase_timestamps_to_conversation_start(
    words_data: list,
    segments_data: list,
    *,
    capture_clock_offset_seconds: float,
) -> None:
    """Shift capture-clock timestamps onto the conversation's audio clock.

    The provider WebSocket spans every conversation in one capture session, so its
    timestamps use the session's cumulative PCM-duration clock.  The caller resolves
    the exact number of audio seconds before this Conversation's first claimed
    sample.  Never infer that origin from the earliest word: doing so erases real
    leading silence/pre-roll and puts ASR words on a different clock from Pyannote.

    Mutates both lists in place.
    """
    base = float(capture_clock_offset_seconds)
    if not math.isfinite(base) or base < 0:
        raise ValueError("capture_clock_offset_seconds must be finite and non-negative")
    if base == 0:
        return

    def _shift(item: dict) -> None:
        for key in ("start", "end"):
            if isinstance(item.get(key), (int, float)):
                item[key] = max(0.0, item[key] - base)

    for w in words_data:
        _shift(w)
    for s in segments_data:
        _shift(s)
        for sw in s.get("words", []) or []:
            _shift(sw)


def _streaming_clock_metadata(
    capture_session_id: str,
    *,
    timestamp_clock: str,
    timestamp_rebase_seconds: float,
) -> dict[str, Any]:
    """Describe the source and projected clocks on a streaming transcript."""
    return {
        "source_timestamp_clock": "capture_session",
        "timestamp_clock": timestamp_clock,
        "timestamp_rebase_seconds": float(timestamp_rebase_seconds),
        "capture_session_id": capture_session_id,
    }


async def _resolve_live_capture_clock_offset(state: ConversationState) -> float:
    """Resolve and cache the live Conversation's exact capture-clock origin.

    A detected Conversation has no durable range claim until finalization.  Build a
    temporary claim over the audio persisted so far, using the same claim builder as
    finalization, and do not expose timed live data until the persisted prefix proves
    the provider-to-Conversation transform.
    """
    if state.capture_clock_offset_seconds is not None:
        return state.capture_clock_offset_seconds

    conversation = await Conversation.find_one(
        Conversation.conversation_id == state.conversation_id
    )
    if conversation is None:
        raise AudioClaimError(
            f"Conversation {state.conversation_id} disappeared before live projection"
        )

    ranges = list(conversation.audio_ranges or [])
    if not ranges:
        started_at = conversation.started_at
        if started_at is None:
            raise AudioClaimError(
                f"Conversation {state.conversation_id} has no semantic start"
            )
        ended_at = datetime.now(timezone.utc)
        ranges = await claim_capture_window(state.session_id, started_at, ended_at)

    offset = await capture_clock_offset_for_ranges(state.session_id, ranges)
    state.capture_clock_offset_seconds = offset
    return offset


async def _save_streaming_transcript(
    session_id: str,
    conversation_id: str,
    aggregator,
) -> str:
    """Retrieve final streaming transcript and save it to the conversation document.

    Gets the combined transcription results from the aggregator, converts them
    to Word and SpeakerSegment model objects, creates a transcript version, and
    saves to MongoDB.

    Returns:
        version_id of the saved transcript version.
    """
    logger.info(
        f"📝 Retrieving final streaming transcript for conversation {conversation_id[:12]}"
    )
    final_transcript = await aggregator.get_combined_results(session_id)

    # Fetch conversation from database to ensure we have latest state
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"❌ Conversation {conversation_id} not found in database")
        raise ValueError(f"Conversation {conversation_id} not found")

    # Remove live-v0 version if it exists (replaced by final streaming version)
    live_removed = False
    conversation.transcript_versions = [
        v for v in conversation.transcript_versions if v.version_id != "live-v0"
    ]
    if conversation.active_transcript_version == "live-v0":
        conversation.active_transcript_version = None
        live_removed = True
    if live_removed:
        logger.info(
            f"🔄 Removed live-v0 transcript version, replacing with final streaming version"
        )

    # Create transcript version from streaming results
    version_id = f"streaming_{session_id[:12]}"
    transcript_text = final_transcript.get("text", "")
    words_data = final_transcript.get("words", [])  # All words from aggregator

    # Provider timestamps use the capture session's cumulative audio-duration clock.
    # Resolve the Conversation's origin on that same clock from its immutable chunk
    # claim.  Wall time is not interchangeable here because capture ranges can contain
    # real gaps or overlaps.  An audio-less salvage has no WAV/claim to align against,
    # so its capture-session timestamps are preserved rather than inventing an origin.
    capture_clock_offset_seconds = 0.0
    timestamp_clock = "capture_session"
    if conversation.audio_ranges:
        capture_clock_offset_seconds = await capture_clock_offset_for_ranges(
            session_id, conversation.audio_ranges
        )
        timestamp_clock = "conversation"
    else:
        logger.warning(
            "Conversation %s has no audio claim; preserving capture-session "
            "transcript timestamps",
            conversation_id[:12],
        )
    timestamp_metadata = _streaming_clock_metadata(
        session_id,
        timestamp_clock=timestamp_clock,
        timestamp_rebase_seconds=capture_clock_offset_seconds,
    )

    # Mutates words_data and the segments list (the same objects are reused below).
    _rebase_timestamps_to_conversation_start(
        words_data,
        final_transcript.get("segments", []),
        capture_clock_offset_seconds=capture_clock_offset_seconds,
    )

    # Convert words to Word objects (including per-word speaker labels if present)
    words = [
        Conversation.Word(
            word=w.get("word", ""),
            start=w.get("start", 0.0),
            end=w.get("end", 0.0),
            confidence=w.get("confidence"),
            speaker=w.get("speaker"),
            speaker_confidence=w.get("speaker_confidence"),
        )
        for w in words_data
    ]

    # Use provider-supplied segments if available (from streaming diarization).
    segments_data = final_transcript.get("segments", [])
    if segments_data:
        segments = [
            Conversation.SpeakerSegment(
                start=s.get("start", 0.0),
                end=s.get("end", 0.0),
                text=s.get("text", ""),
                speaker=str(s.get("speaker", "Unknown")),
                words=[
                    Conversation.Word(
                        word=sw.get("word", ""),
                        start=sw.get("start", 0.0),
                        end=sw.get("end", 0.0),
                        confidence=sw.get("confidence"),
                        speaker=sw.get("speaker"),
                        speaker_confidence=sw.get("speaker_confidence"),
                    )
                    for sw in s.get("words", [])
                ],
            )
            for s in segments_data
        ]
        provider_diarized = True
    else:
        # Non-diarizing streaming provider returned no segments. Build a single
        # fallback segment from the transcript text/words so the transcript is always
        # renderable in the UI (the frontend only shows the segment list). This mirrors
        # the batch path's fallback. If speaker recognition is enabled, the
        # post-conversation speaker job replaces this with diarized segments.
        provider_diarized = False
        if transcript_text:
            start_time_audio = words_data[0].get("start", 0.0) if words_data else 0.0
            end_time_audio = words_data[-1].get("end", 0.0) if words_data else 0.0
            segments = [
                Conversation.SpeakerSegment(
                    speaker="Speaker 0",
                    start=start_time_audio,
                    end=end_time_audio,
                    text=transcript_text,
                    words=words,
                )
            ]
        else:
            segments = []

    # Provenance as reported by the producer. No "deepgram" default: guessing a
    # provider is worse than recording that it is unknown, because the guess is
    # indistinguishable from a real Deepgram transcript afterwards.
    provider = final_transcript.get("provider") or "unknown"
    mode = final_transcript.get("mode") or "streaming"
    model = final_transcript.get("model") or provider

    # Diarization source reflects real provider diarization, not the fallback segment
    diarization_source = "provider" if provider_diarized else None

    # Add streaming transcript with words at version level
    version = conversation.add_transcript_version(
        version_id=version_id,
        transcript=transcript_text,
        words=words,  # Store at version level
        segments=segments,  # Provider segments or empty (filled by speaker service later)
        provider=provider,
        model=model,
        processing_time_seconds=None,  # Not applicable for streaming
        metadata={
            "source": "streaming",
            "mode": mode,
            "chunk_count": final_transcript.get("chunk_count", 0),
            "word_count": len(words),
            **timestamp_metadata,
            "provider_capabilities": {"diarization": provider_diarized},
        },
        set_as_active=True,
    )
    version.diarization_source = diarization_source

    transcript_artifact_ids: list[str] = []
    if conversation.audio_ranges:
        transcript_artifact = await persist_transcript_artifact(
            user_id=str(conversation.user_id),
            audio_ranges=conversation.audio_ranges,
            retry_key=f"streaming-transcription:{conversation_id}:{version_id}",
            provider=provider,
            model=model,
            transcript=transcript_text,
            words=words_data,
            segments=segments_data,
            raw_response={
                "source": "streaming",
                "mode": mode,
                "chunk_count": final_transcript.get("chunk_count", 0),
                **timestamp_metadata,
            },
        )
        transcript_artifact_ids = [transcript_artifact.artifact_id]
        version.metadata["transcript_artifact_id"] = transcript_artifact.artifact_id
        version.metadata["transcript_artifact_ids"] = transcript_artifact_ids
    await persist_conversation_revision(
        conversation,
        version,
        retry_key=f"streaming-projection:{conversation_id}:{version_id}",
        transcript_artifact_ids=transcript_artifact_ids,
    )

    # Save conversation with streaming transcript
    await conversation.save()
    if provider_diarized:
        segment_info = f"{len(segments)} provider segments (diarization_source={diarization_source})"
    elif segments:
        segment_info = f"{len(segments)} fallback segment (pending speaker recognition)"
    else:
        segment_info = "0 segments (empty transcript)"
    logger.info(
        f"✅ Saved streaming transcript: {len(transcript_text)} chars, "
        f"{segment_info}, {len(words)} words "
        f"for conversation {conversation_id[:12]}"
    )

    return version_id


async def _enqueue_post_processing(
    conversation_id: str,
    user_id: str,
    client_id: str,
    version_id: str,
    end_reason: str,
    memory_space_id: str | None = None,
) -> None:
    """Enqueue post-conversation processing jobs (speaker, memory, title, events).

    Checks configuration for always_batch_retranscribe. If enabled, enqueues
    a batch transcription job first with post-processing depending on it.
    Otherwise starts post-processing immediately with the streaming transcript.
    """
    transcription_cfg = get_backend_config("transcription")
    batch_retranscribe = False
    if transcription_cfg:
        cfg_dict = OmegaConf.to_container(transcription_cfg, resolve=True)
        batch_retranscribe = cfg_dict.get("always_batch_retranscribe", False)

    if batch_retranscribe:
        # BATCH PATH: Streaming transcript saved as preview — user sees it immediately
        # Full post-processing (speaker, memory, title) waits for batch transcript
        # Lazy import: circular dependency — transcription_jobs imports conversation_jobs.
        from backend.workers.transcription_jobs import transcribe_full_audio_job

        batch_version_id = f"batch_{conversation_id[:12]}"
        batch_job = transcription_queue.enqueue(
            transcribe_full_audio_job,
            conversation_id,
            batch_version_id,
            "always_batch_retranscribe",
            job_timeout=-1,
            result_ttl=JOB_RESULT_TTL,
            job_id=f"batch_retranscribe_{conversation_id[:12]}",
            description=f"Batch re-transcription for {conversation_id[:8]}",
            meta={"conversation_id": conversation_id, "client_id": client_id},
        )

        logger.info(
            f"🔄 Batch re-transcribe enabled: enqueued batch job {batch_job.id} "
            f"(streaming transcript is preview only)"
        )

        # Run post-processing ONLY after batch completes
        job_ids = start_post_conversation_jobs(
            conversation_id=conversation_id,
            user_id=user_id,
            transcript_version_id=batch_version_id,
            depends_on_job=batch_job,
            client_id=client_id,
            end_reason=end_reason,
            trigger=Conversation.ProcessingTrigger.LIVE_SESSION.value,
            memory_space_id=memory_space_id,
        )

        logger.info(
            f"📥 Pipeline: batch_retranscribe({batch_job.id}) → "
            f"speaker({job_ids['speaker_recognition']}) → "
            f"memory({job_ids['memory']}) → summary_bundle("
            f"{job_ids['title']}, {job_ids['short_summary']}, "
            f"{job_ids['detailed_summary']}) → "
            f"event({job_ids['event_dispatch']})"
        )
    else:
        # NORMAL PATH: Process streaming transcript immediately (existing behavior)
        job_ids = start_post_conversation_jobs(
            conversation_id=conversation_id,
            user_id=user_id,
            transcript_version_id=version_id,  # Pass the streaming transcript version ID
            depends_on_job=None,  # No dependency - streaming already succeeded
            client_id=client_id,  # Pass client_id for UI tracking
            end_reason=end_reason,  # Pass the determined end_reason (websocket_disconnect, inactivity_timeout, etc.)
            trigger=Conversation.ProcessingTrigger.LIVE_SESSION.value,
            memory_space_id=memory_space_id,
        )

        logger.info(
            f"📥 Pipeline: speaker({job_ids['speaker_recognition']}) → "
            f"memory({job_ids['memory']}) → summary_bundle("
            f"{job_ids['title']}, {job_ids['short_summary']}, "
            f"{job_ids['detailed_summary']}) → "
            f"event({job_ids['event_dispatch']})"
        )

    # Wait a moment to ensure jobs are registered in RQ
    await asyncio.sleep(0.5)

    logger.info(
        f"✅ Post-conversation pipeline started with event dispatch job (end_reason={end_reason})"
    )


@async_job(redis=True, beanie=True)
async def open_conversation_job(
    session_id: str,
    user_id: str,
    client_id: str,
    speech_detected_at: float,
    speech_job_id: Optional[str] = None,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    Long-running RQ job that creates and continuously updates conversation with transcription results.

    Creates conversation when speech is detected, then monitors and updates until session ends.

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        speech_detected_at: Timestamp when speech was first detected
        speech_job_id: Optional speech detection job ID to update with conversation_id
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with conversation_id, final_result_count, runtime_seconds

    Note: user_email is fetched from the database when needed.
    """
    logger.info(
        f"📝 Creating and opening conversation for session {session_id} (speech detected at {speech_detected_at})"
    )

    # Phase 1: Initialize job and conversation
    current_job = get_current_job()
    current_job.meta = {}
    current_job.save_meta()

    conversation_id = await _initialize_conversation(
        session_id=session_id,
        user_id=user_id,
        client_id=client_id,
        speech_job_id=speech_job_id,
        speech_detected_at=speech_detected_at,
        current_job=current_job,
        redis_client=redis_client,
    )

    # Phase 2: Monitor conversation (polling loop)
    aggregator = TranscriptionResultsAggregator(redis_client)
    session_store = SessionStore(redis_client)
    read_session = getattr(session_store, "read", None)
    session_view = await read_session(session_id) if read_session is not None else None
    state = ConversationState(
        conversation_id=conversation_id,
        session_id=session_id,
        user_id=user_id,
        client_id=client_id,
        memory_space_id=(session_view.memory_space_id if session_view else ""),
        start_time=time.time(),
    )

    await _monitor_conversation_loop(state, aggregator, current_job, redis_client)

    logger.info(
        f"✅ Conversation {conversation_id} updates complete, checking for meaningful speech..."
    )

    # Phase 3: Determine end reason
    completion_reason_str = await SessionStore(redis_client).get_completion_reason(
        session_id
    )

    if completion_reason_str:
        state.end_reason = completion_reason_str
        logger.info(f"📊 Using completion_reason from session: {state.end_reason}")
    elif state.close_requested_reason:
        state.end_reason = "close_requested"
        logger.info(
            f"📊 Conversation closed by request: {state.close_requested_reason}"
        )
    elif state.timeout_triggered:
        state.end_reason = "inactivity_timeout"
    elif time.time() - state.start_time > 86400:
        state.end_reason = "max_duration"
    else:
        state.end_reason = "user_stopped"

    logger.info(
        f"📊 Conversation {conversation_id[:12]} end_reason determined: {state.end_reason}"
    )

    # Phase 4-7: Post-processing (wrapped in try/finally for guaranteed cleanup)
    end_of_conversation_handled = False
    try:
        logger.info(
            "✅ Conversation has meaningful speech (validated during streaming), proceeding with post-processing"
        )

        # Phase 4: Wait for streaming transcription to complete
        if state.close_requested_reason:
            logger.info(
                f"⏩ Skipping transcription:complete wait for close_requested "
                f"(reason={state.close_requested_reason})"
            )
        else:
            completion_key = f"transcription:complete:{session_id}"
            max_wait_streaming = 30  # seconds
            waited_streaming = 0.0
            while waited_streaming < max_wait_streaming:
                completion_status = await redis_client.get(completion_key)
                if completion_status:
                    status_str = (
                        completion_status.decode()
                        if isinstance(completion_status, bytes)
                        else completion_status
                    )
                    if status_str == "error":
                        logger.warning(
                            f"⚠️ Streaming transcription ended with error for {session_id}, proceeding anyway"
                        )
                    else:
                        logger.info(
                            f"✅ Streaming transcription confirmed complete for {session_id}"
                        )
                    break
                await asyncio.sleep(0.5)
                waited_streaming += 0.5

            if waited_streaming >= max_wait_streaming:
                logger.warning(
                    f"⚠️ Timed out waiting for streaming completion signal for {session_id} "
                    f"(waited {max_wait_streaming}s), proceeding with available transcript"
                )

        # Phase 5: Claim the persisted capture interval. This changes only the
        # semantic object; the underlying capture chunks are never reassigned.
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if conversation is None:
            raise RuntimeError(f"Conversation {conversation_id} disappeared")
        claim_end = datetime.now(timezone.utc)
        chunks_ready = await _claim_persisted_capture(
            conversation,
            session_id,
            claim_end,
            max_wait_seconds=30,
        )

        if not chunks_ready:
            # The capture never reached Mongo. Preserve a real transcript, but do not
            # fabricate an audio claim.
            salvage_conv = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            has_transcript = bool(
                salvage_conv and salvage_conv.has_meaningful_transcript
            )

            if should_discard_unbacked_conversation(has_transcript):
                # Genuinely empty (no transcript, no audio). A websocket_disconnect is a
                # benign network drop (warning, not an error-level system event); a clean
                # end with nothing captured is more unexpected (error).
                if state.end_reason == "websocket_disconnect":
                    logger.warning(
                        f"⚠️ No audio and no transcript for conversation "
                        f"{conversation_id[:12]} (client disconnect) — discarding as "
                        f"audio_chunks_not_ready (likely a network drop, not a fault)"
                    )
                else:
                    logger.error(
                        f"❌ Audio chunks not found after 30s for conversation "
                        f"{conversation_id[:12]} (end_reason={state.end_reason}) — "
                        f"discarding as audio_chunks_not_ready"
                    )
                await mark_conversation_deleted(
                    conversation_id=conversation_id,
                    deletion_reason="audio_chunks_not_ready",
                )
            else:
                # Salvage: persist the streaming transcript so it survives as a proper
                # version, and KEEP the conversation. Skip the audio-dependent
                # post-conversation chain (batch re-transcribe would raise without audio).
                logger.warning(
                    f"🛟 No audio persisted for conversation {conversation_id[:12]} but it "
                    f"has a real transcript — keeping it as an audio-less semantic "
                    f"record instead of discarding it"
                )
                try:
                    await _save_streaming_transcript(
                        session_id=session_id,
                        conversation_id=conversation_id,
                        aggregator=aggregator,
                    )
                except Exception as e:  # noqa: BLE001 — salvage must not raise
                    logger.warning(
                        f"Could not persist streaming transcript for salvaged "
                        f"conversation {conversation_id[:12]}: {e}"
                    )

            end_of_conversation_handled = True
            return await handle_end_of_conversation(
                session_id=session_id,
                conversation_id=conversation_id,
                client_id=client_id,
                user_id=user_id,
                start_time=state.start_time,
                last_result_count=state.last_result_count,
                timeout_triggered=state.timeout_triggered,
                redis_client=redis_client,
                end_reason=state.end_reason,
            )

        logger.info(
            f"📦 MongoDB audio chunks ready for conversation {conversation_id[:12]}"
        )

        # Phase 6: Save streaming transcript
        version_id = await _save_streaming_transcript(
            session_id=session_id,
            conversation_id=conversation_id,
            aggregator=aggregator,
        )

        # Phase 6b: Trim the conversation's silence. Shared with the batch-fallback
        # finalization path — see maybe_trim_silence.
        await maybe_trim_silence(conversation_id)

        # Phase 7: Enqueue post-processing pipeline
        await _enqueue_post_processing(
            conversation_id=conversation_id,
            user_id=user_id,
            client_id=client_id,
            version_id=version_id,
            end_reason=state.end_reason,
            memory_space_id=state.memory_space_id or None,
        )

        # Cleanup and session restart
        end_of_conversation_handled = True
        return await handle_end_of_conversation(
            session_id=session_id,
            conversation_id=conversation_id,
            client_id=client_id,
            user_id=user_id,
            start_time=state.start_time,
            last_result_count=state.last_result_count,
            timeout_triggered=state.timeout_triggered,
            redis_client=redis_client,
            end_reason=state.end_reason,
        )
    finally:
        if not end_of_conversation_handled:
            logger.error(
                f"⚠️ open_conversation_job post-processing failed for {conversation_id}, "
                f"performing emergency cleanup to re-enable speech detection"
            )
            try:
                await handle_end_of_conversation(
                    session_id=session_id,
                    conversation_id=conversation_id,
                    client_id=client_id,
                    user_id=user_id,
                    start_time=state.start_time,
                    last_result_count=state.last_result_count,
                    timeout_triggered=state.timeout_triggered,
                    redis_client=redis_client,
                    end_reason="error",
                )
            except Exception as cleanup_error:
                logger.error(f"❌ Emergency cleanup also failed: {cleanup_error}")


async def _get_summary_job_input(
    conversation_id: str, stage: str
) -> tuple[Optional[Conversation], str, list, Optional[Dict[str, Any]]]:
    """Load one summary-stage input and apply common skip/error rules."""
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return None, "", [], {"success": False, "error": "Conversation not found"}
    # Memory eligibility and recording metadata are separate concerns. Detected
    # continuous-capture conversations deliberately opt out of per-conversation
    # memory while remaining user-visible recordings that need titles/summaries.
    if conversation.memory_excluded and conversation.data_purpose != "conversation":
        logger.info(
            f"Skipping {stage} generation for memory-excluded conversation "
            f"{conversation_id[:8]}"
        )
        return (
            conversation,
            "",
            [],
            {
                "success": True,
                "skipped": True,
                "reason": "memory_excluded",
                "conversation_id": conversation_id,
            },
        )

    transcript_text = conversation.transcript or ""
    segments = conversation.segments or []
    if not transcript_text and not segments:
        logger.warning(
            f"No transcript or segments available for {stage} generation on "
            f"conversation {conversation_id}"
        )
        return (
            conversation,
            transcript_text,
            segments,
            {
                "success": False,
                "error": "No transcript or segments available",
                "conversation_id": conversation_id,
            },
        )

    set_otel_session(conversation_id)
    set_span_attrs(user_id=str(conversation.user_id))
    set_trace_io(input={"transcript": transcript_text})
    return conversation, transcript_text, segments, None


def _publish_summary_update(conversation: Conversation) -> None:
    publish_sse_event(
        str(conversation.user_id),
        "conversation.updated",
        {
            "conversation_id": conversation.conversation_id,
            "title": conversation.title,
            "summary": conversation.summary,
        },
    )


def _provider_permission_result(conversation_id: str, stage: str) -> Dict[str, Any]:
    """Finish a deterministic provider/config failure without spending RQ retries."""
    result = {
        "success": False,
        "conversation_id": conversation_id,
        "stage": stage,
        "reason": "provider_permission_denied",
        "retryable": False,
    }
    update_job_meta(**result)
    set_trace_io(output=result)
    logger.warning(
        "Skipping %s for %s: provider denied the request; retry requires a "
        "configuration or allowance change",
        stage,
        conversation_id,
    )
    return result


@async_job(redis=True, beanie=True)
@traced_job("title", pipeline_stage="title", gen_ai_operation="chat")
async def generate_title_job(
    conversation_id: str, *, redis_client=None
) -> Dict[str, Any]:
    """Generate and persist only the conversation title."""
    started_at = time.time()
    conversation, transcript_text, segments, early_result = (
        await _get_summary_job_input(conversation_id, "title")
    )
    if early_result is not None:
        return early_result
    assert conversation is not None

    try:
        title = await generate_conversation_title(
            transcript_text,
            segments=segments,
            user_id=conversation.user_id,
        )
    except openai.PermissionDeniedError:
        return _provider_permission_result(conversation_id, "title")
    if title == TITLE_NOT_GENERATED or not title.strip():
        raise RuntimeError(
            f"Title generation returned the missing-title placeholder for {conversation_id}"
        )
    conversation.title = title
    await conversation.save()
    _publish_summary_update(conversation)
    processing_time = time.time() - started_at
    update_job_meta(
        conversation_id=conversation_id,
        title=title,
        segment_count=len(segments),
        processing_time=processing_time,
    )
    result = {
        "success": True,
        "conversation_id": conversation_id,
        "title": title,
        "processing_time_seconds": processing_time,
    }
    set_trace_io(output=result)
    logger.info(f"Generated title for {conversation_id} in {processing_time:.2f}s")
    return result


@async_job(redis=True, beanie=True)
@traced_job("short_summary", pipeline_stage="short_summary", gen_ai_operation="chat")
async def generate_short_summary_job(
    conversation_id: str, *, redis_client=None
) -> Dict[str, Any]:
    """Generate and persist only the short conversation summary."""
    started_at = time.time()
    conversation, transcript_text, segments, early_result = (
        await _get_summary_job_input(conversation_id, "short summary")
    )
    if early_result is not None:
        return early_result
    assert conversation is not None

    try:
        summary = await generate_short_summary(
            transcript_text,
            segments=segments,
            user_id=conversation.user_id,
        )
    except openai.PermissionDeniedError:
        return _provider_permission_result(conversation_id, "short_summary")
    conversation.summary = summary
    await conversation.save()
    _publish_summary_update(conversation)
    processing_time = time.time() - started_at
    update_job_meta(
        conversation_id=conversation_id,
        summary=summary,
        segment_count=len(segments),
        processing_time=processing_time,
    )
    result = {
        "success": True,
        "conversation_id": conversation_id,
        "summary": summary,
        "processing_time_seconds": processing_time,
    }
    set_trace_io(output=result)
    logger.info(
        f"Generated short summary for {conversation_id} in {processing_time:.2f}s"
    )
    return result


@async_job(redis=True, beanie=True)
@traced_job(
    "detailed_summary", pipeline_stage="detailed_summary", gen_ai_operation="chat"
)
async def generate_detailed_summary_job(
    conversation_id: str,
    *,
    include_memory_context: bool = True,
    redis_client=None,
) -> Dict[str, Any]:
    """Generate and persist only the detailed conversation summary."""
    started_at = time.time()
    conversation, transcript_text, segments, early_result = (
        await _get_summary_job_input(conversation_id, "detailed summary")
    )
    if early_result is not None:
        return early_result
    assert conversation is not None

    memory_context = None
    if include_memory_context:
        try:
            memory_service = get_memory_service()
            memories = await memory_service.search_memories(
                transcript_text,
                conversation.user_id,
                limit=10,
                memory_space_id=conversation.memory_space_id,
            )
            if memories:
                memory_context = "\n".join(m.content for m in memories if m.content)
        except Exception as mem_error:
            logger.warning(
                f"Could not fetch detailed-summary memory context (continuing without): "
                f"{mem_error}"
            )
    else:
        logger.info("Skipping vault retrieval for bulk timeline promotion")

    try:
        detailed_summary = await generate_detailed_summary(
            transcript_text,
            segments=segments,
            memory_context=memory_context,
        )
    except openai.PermissionDeniedError:
        return _provider_permission_result(conversation_id, "detailed_summary")
    conversation.detailed_summary = detailed_summary
    await conversation.save()
    _publish_summary_update(conversation)
    processing_time = time.time() - started_at
    update_job_meta(
        conversation_id=conversation_id,
        detailed_summary_length=len(detailed_summary),
        segment_count=len(segments),
        processing_time=processing_time,
    )
    result = {
        "success": True,
        "conversation_id": conversation_id,
        "detailed_summary": detailed_summary,
        "processing_time_seconds": processing_time,
    }
    set_trace_io(output=result)
    logger.info(
        f"Generated detailed summary for {conversation_id} in {processing_time:.2f}s"
    )
    return result


@async_job(redis=True, beanie=True)
async def dispatch_conversation_complete_event_job(
    conversation_id: str,
    client_id: str,
    user_id: str,
    end_reason: Optional[str] = None,
    trigger: str = Conversation.ProcessingTrigger.FILE_UPLOAD.value,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    Dispatch conversation.complete plugin event for all conversation sources.

    This job runs at the end of conversation processing to ensure plugins
    receive the conversation.complete event with the correct end_reason.
    Used by both file upload and WebSocket streaming paths.

    Args:
        conversation_id: Conversation ID
        client_id: Client ID
        user_id: User ID
        end_reason: Why the *recording* ended — a ``Conversation.EndReason`` value, or
            None when nothing ended now. A reprocess passes None so the original
            reason survives, and an upload passes None because a file never ended for
            an operational reason.
        trigger: Why the *pipeline* is running — a ``Conversation.ProcessingTrigger``
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with success status and plugin results
    """
    logger.info(
        f"📌 Dispatching conversation.complete event for conversation {conversation_id}"
    )

    start_time = time.time()

    # Get the conversation to include in event data
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    # This is the terminal job of the post-conversation chain, so it OWNS the
    # conversation's final state: persist end_reason/completed_at and reconcile
    # processing_status from facts (transcript present => completed; none =>
    # failed). Intermediate jobs no longer stamp the status, which is what kept
    # recovered conversations stuck at "failed". end_reason is persisted before
    # plugins receive the conversation.complete event.
    needs_save = False
    if end_reason and conversation.end_reason is None:
        try:
            conversation.end_reason = Conversation.EndReason(end_reason)
        except ValueError:
            # No longer reachable from a processing trigger — those travel in
            # ``trigger`` now. A value landing here is a caller passing something that
            # is not an EndReason at all, which UNKNOWN would quietly absorb.
            logger.error(
                "⚠️ %s is not a Conversation.EndReason (conversation %s); "
                "storing UNKNOWN",
                end_reason,
                conversation_id,
            )
            conversation.end_reason = Conversation.EndReason.UNKNOWN
        needs_save = True

    if conversation.completed_at is None:
        conversation.completed_at = datetime.now(timezone.utc)
        needs_save = True

    if conversation.apply_status(settled=True):
        logger.info(
            f"🏁 Finalized conversation {conversation_id[:12]} "
            f"status={conversation.processing_status}"
            + (
                f" failure_stage={conversation.failure_stage}"
                if conversation.failure_stage
                else ""
            )
        )
        needs_save = True

    if needs_save:
        await conversation.save()

    # Context collection is purpose-bound: only after a conversation has settled
    # do we ask connected ScreenPipe companions for this time range.
    try:

        await request_conversation_context_jobs(conversation)
    except Exception as exc:
        logger.warning(
            "Failed to request device context for conversation %s: %s",
            conversation_id,
            exc,
        )

    # The conversation's own reason, which a reprocess leaves untouched — not the
    # trigger that started this run.
    settled_end_reason = (
        conversation.end_reason.value if conversation.end_reason else None
    )

    if conversation.memory_excluded:
        logger.info(
            f"Skipping conversation.complete plugins for memory-excluded conversation {conversation_id[:8]}"
        )
        publish_sse_event(
            user_id,
            "conversation.completed",
            {
                "conversation_id": conversation_id,
                "end_reason": settled_end_reason,
                "trigger": trigger,
            },
        )
        return {
            "success": True,
            "skipped": True,
            "reason": "memory_excluded",
            "conversation_id": conversation_id,
            "processing_time_seconds": time.time() - start_time,
        }

    # Get user email for event data
    user = await User.get(user_id)
    user_email = user.email if user else ""

    # Prepare plugin event data (same format as open_conversation_job)
    try:
        plugin_results = await dispatch_or_defer_space_event(
            event=PluginEvent.CONVERSATION_COMPLETE,
            user_id=user_id,
            memory_space_id=(conversation.memory_space_id if conversation else None),
            source_kind="conversation",
            source_id=conversation_id,
            data={
                "conversation": {
                    "client_id": client_id,
                    "user_id": user_id,
                },
                "transcript": conversation.transcript if conversation else "",
                "duration": 0,  # Duration not tracked for file uploads
                "conversation_id": conversation_id,
            },
            metadata={"end_reason": settled_end_reason, "trigger": trigger},
            description=(
                f"conversation={conversation_id[:12]}, "
                f"end_reason={settled_end_reason}, trigger={trigger}"
            ),
            require_router=True,
        )

        processing_time = time.time() - start_time
        logger.info(
            f"✅ Conversation complete event dispatched for {conversation_id} in {processing_time:.2f}s"
        )

        publish_sse_event(
            user_id,
            "conversation.completed",
            {
                "conversation_id": conversation_id,
                "end_reason": settled_end_reason,
                "trigger": trigger,
            },
        )

        return {
            "success": True,
            "conversation_id": conversation_id,
            "plugin_count": len(plugin_results) if plugin_results else 0,
            "processing_time_seconds": processing_time,
        }

    except RuntimeError as e:
        logger.error(f"❌ {e}")
        return {
            "success": False,
            "skipped": True,
            "reason": "No plugin router",
            "conversation_id": conversation_id,
            "error": str(e),
        }
    except Exception as e:
        logger.warning(f"⚠️ Error dispatching conversation complete event: {e}")
        return {
            "success": False,
            "error": str(e),
            "conversation_id": conversation_id,
        }
