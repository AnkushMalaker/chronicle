"""
Data-audit controller.

Backs the Data Audit dashboard page: surfaces per-conversation VAD speech
metrics + latest speaker labels, filters by a compound predicate (speech
fraction AND speaker include/exclude), enqueues batch audio analysis,
archives (hard-deletes) audio, and splits/merges conversations.
"""

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import FileResponse, JSONResponse

from advanced_omi_backend.controllers.conversation_controller import (
    archive_conversation_audio,
)
from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    default_queue,
    start_post_conversation_jobs,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation, create_conversation
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.users import User
from advanced_omi_backend.utils.annotation_export import (
    EXPORTS_DIR,
    META_NAME,
    ZIP_NAME,
    export_dir,
    new_export_id,
    validate_export_id,
)
from advanced_omi_backend.utils.transcript_slicing import (
    build_transcript_text,
    shift_segments,
    shift_words,
    slice_segments,
    slice_words,
)
from advanced_omi_backend.utils.vad_analysis import (
    detect_silence_gaps,
    frame_speech_intervals,
    intersect_intervals,
    merge_speech_regions,
    speech_fraction_from_histogram,
)
from advanced_omi_backend.workers.data_audit_jobs import (
    analyze_audio_batch_job,
    auto_clean_job,
    export_annotation_dataset_job,
    get_sensitivity_policy,
    screen_conversations_job,
)

logger = logging.getLogger(__name__)

# Upper bound on how many conversations a single scan inspects in memory.
# Speaker/silence predicates are applied in Python, so we cap the working set
# and report when it was hit rather than silently truncating.
MAX_SCAN = 2000

# Projection: lightweight metadata + cached VAD analysis + speaker labels
# from the active transcript version's segments (no transcript text / words).
_SCAN_PROJECTION = {
    "conversation_id": 1,
    "user_id": 1,
    "client_id": 1,
    "title": 1,
    "created_at": 1,
    "audio_total_duration": 1,
    "audio_chunks_count": 1,
    "audio_archived": 1,
    "audio_archived_at": 1,
    "archive_reason": 1,
    "vad_analysis": 1,
    "derived_from": 1,
    "active_transcript_version": 1,
    "transcript_versions.version_id": 1,
    "transcript_versions.segments.speaker": 1,
    "transcript_versions.segments.identified_as": 1,
}


def _speakers_for_doc(doc: dict) -> List[str]:
    """Distinct speaker labels from the active transcript version's segments.

    Prefers ``identified_as`` (recognized name) over the raw ``speaker`` label.
    """
    active = doc.get("active_transcript_version")
    speakers: set = set()
    for tv in doc.get("transcript_versions") or []:
        if tv.get("version_id") != active:
            continue
        for seg in tv.get("segments") or []:
            label = seg.get("identified_as") or seg.get("speaker")
            if label:
                speakers.add(label)
        break
    return sorted(speakers)


async def list_for_audit(
    user: User,
    speech_threshold: float = 0.5,
    min_speech_fraction: float = 0.0,
    max_speech_fraction: float = 1.0,
    min_duration: float = 0.0,
    max_duration: float = 0.0,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    include_speakers: Optional[List[str]] = None,
    exclude_speakers: Optional[List[str]] = None,
    archived_only: bool = False,
    limit: int = 100,
    offset: int = 0,
):
    """List conversations with VAD speech metrics + speakers, filtered by the
    compound predicate. Returns derived ``speech_fraction`` at the requested
    probability threshold so the UI threshold control is just a re-fetch.

    Speaker filtering is per-speaker tri-state: a conversation is kept only if
    it contains at least one ``include_speakers`` (when any are set) AND none of
    the ``exclude_speakers``. Speech bounds exclude unanalyzed conversations;
    ``max_speech_fraction=1`` / ``min_speech_fraction=0`` / ``max_duration=0``
    disable the respective bound.
    """
    try:
        base: dict = {} if user.is_superuser else {"user_id": str(user.user_id)}

        if archived_only:
            base["audio_archived"] = True
        else:
            # audio_archived is a newer field; absent == not archived
            base["audio_archived"] = {"$ne": True}
            base["deleted"] = {"$ne": True}
            base["audio_chunks_count"] = {"$gt": 0}

        # Date range goes into the Mongo query (not the Python predicate) so
        # it narrows the MAX_SCAN working set instead of competing with it.
        if created_after or created_before:
            created: dict = {}
            if created_after:
                created["$gte"] = created_after
            if created_before:
                created["$lte"] = created_before
            base["created_at"] = created

        collection = Conversation.get_pymongo_collection()
        cursor = (
            collection.find(base, _SCAN_PROJECTION)
            .sort("created_at", -1)
            .limit(MAX_SCAN)
        )
        raw_docs = await cursor.to_list(length=MAX_SCAN)
        scan_capped = len(raw_docs) >= MAX_SCAN

        include_set = set(include_speakers or [])
        exclude_set = set(exclude_speakers or [])
        matched: List[dict] = []
        # Speakers present anywhere in the scanned working set (before the
        # compound predicate), so the filter UI offers exactly the labels that
        # exist in this view — and isn't narrowed by its own selection.
        available_speakers: set = set()

        for doc in raw_docs:
            duration = doc.get("audio_total_duration") or 0.0
            va = doc.get("vad_analysis")
            doc_speakers = _speakers_for_doc(doc)
            available_speakers.update(doc_speakers)

            speech_fraction = None
            if va:
                speech_fraction = speech_fraction_from_histogram(
                    histogram=va.get("histogram") or [],
                    frame_count=va.get("frame_count") or 0,
                    histogram_bin_width=va.get("histogram_bin_width") or 0.05,
                    threshold=speech_threshold,
                )

            # --- Compound filter (skip filters for the archived audit view) ---
            if not archived_only:
                if duration < min_duration:
                    continue
                if 0.0 < max_duration < duration:
                    continue
                if max_speech_fraction < 1.0 or min_speech_fraction > 0.0:
                    # Unanalyzed conversations can't satisfy a speech filter
                    if speech_fraction is None:
                        continue
                    if speech_fraction > max_speech_fraction:
                        continue
                    if speech_fraction < min_speech_fraction:
                        continue
                if include_set and not include_set.intersection(doc_speakers):
                    continue
                if exclude_set and exclude_set.intersection(doc_speakers):
                    continue

            created_at = doc.get("created_at")
            archived_at = doc.get("audio_archived_at")
            derived_from = doc.get("derived_from")
            matched.append(
                {
                    "conversation_id": doc.get("conversation_id"),
                    "title": doc.get("title"),
                    "client_id": doc.get("client_id"),
                    "created_at": created_at.isoformat() if created_at else None,
                    "duration_seconds": duration,
                    "speakers": doc_speakers,
                    "analyzed": va is not None,
                    "speech_fraction": (
                        round(speech_fraction, 4)
                        if speech_fraction is not None
                        else None
                    ),
                    "derived_operation": (
                        derived_from.get("operation") if derived_from else None
                    ),
                    "audio_archived": doc.get("audio_archived", False),
                    "audio_archived_at": (
                        archived_at.isoformat() if archived_at else None
                    ),
                    "archive_reason": doc.get("archive_reason"),
                }
            )

        total = len(matched)
        page = matched[offset : offset + limit]

        # How many conversations the Analyze button would actually process:
        # the user's own live audio without cached VAD analysis (the batch job
        # is scoped to the requesting user, so the count matches even for
        # superusers viewing everyone's rows). Lets the UI disable the button
        # when there is nothing left to analyze.
        unanalyzed_count = await collection.count_documents(
            {
                "user_id": str(user.user_id),
                "audio_archived": {"$ne": True},
                "deleted": {"$ne": True},
                "audio_chunks_count": {"$gt": 0},
                "vad_analysis": None,
            }
        )

        return {
            "conversations": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "scan_capped": scan_capped,
            "speech_threshold": speech_threshold,
            "unanalyzed_count": unanalyzed_count,
            "speakers": sorted(available_speakers),
        }

    except Exception as e:
        logger.exception(f"Error listing conversations for cleaning: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error listing conversations"}
        )


async def enqueue_analysis(
    user: User, conversation_ids: Optional[List[str]] = None, force: bool = False
):
    """Enqueue the batch VAD-analysis job for the user's conversations."""
    try:
        # Pass as kwargs so the job's user_id is recorded in job.kwargs — the
        # queue status endpoint authorizes non-admins by job.kwargs["user_id"].
        job = default_queue.enqueue(
            analyze_audio_batch_job,
            user_id=str(user.user_id),
            conversation_ids=conversation_ids,
            force=force,
            job_timeout=3600,
            result_ttl=JOB_RESULT_TTL,
            description="Analyze conversation audio (VAD)",
        )
        logger.info(f"Enqueued VAD analysis job {job.id} for user {user.user_id}")
        return {"job_id": job.id, "status": "queued"}

    except Exception as e:
        logger.exception(f"Error enqueueing VAD analysis: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error enqueueing analysis"}
        )


async def run_auto_clean_cron() -> dict:
    """Cron entrypoint for auto-clean.

    Runs in the FastAPI process (invoked by the cron scheduler). It does NOT do
    the work itself — it enqueues the ``auto_clean_job`` RQ job so the sweep is
    visible on the Queue/Jobs page (and runs in the worker). Returns the
    enqueued job id, which the cron-jobs page records as the run result.
    """
    job = default_queue.enqueue(
        auto_clean_job,
        job_timeout=3600,
        result_ttl=JOB_RESULT_TTL,
        description="Auto-clean: archive speech-free conversations",
    )
    logger.info(f"Auto-clean cron enqueued job {job.id}")
    return {"enqueued_job_id": job.id, "queue": "default"}


async def archive_audio_many(user: User, conversation_ids: List[str], reason: str):
    """Archive (hard-delete) audio for multiple conversations."""
    results = []
    for cid in conversation_ids:
        response = await archive_conversation_audio(cid, user, reason)
        try:
            body = json.loads(bytes(response.body).decode())
        except Exception:
            body = {}
        results.append(
            {
                "conversation_id": cid,
                "status_code": response.status_code,
                "ok": response.status_code == 200,
                "deleted_chunks": body.get("deleted_chunks"),
                "error": body.get("error"),
            }
        )

    archived = sum(1 for r in results if r["ok"])
    return {"archived": archived, "total": len(conversation_ids), "results": results}


# ---------------------------------------------------------------------------
# Split / merge
# ---------------------------------------------------------------------------

# Projection for chunk timeline reads — never pull audio_data.
_CHUNK_META_PROJECTION = {
    "chunk_index": 1,
    "start_time": 1,
    "end_time": 1,
    "duration": 1,
    "vad.max_score": 1,
}


async def _load_operable_conversation(
    user: User, conversation_id: str
) -> Tuple[Optional[Conversation], Optional[JSONResponse]]:
    """Fetch a conversation and validate it can be split/merged.

    Returns (conversation, None) or (None, error_response).
    """
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        return None, JSONResponse(
            status_code=404, content={"error": "Conversation not found"}
        )
    if not user.is_superuser and conversation.user_id != str(user.user_id):
        return None, JSONResponse(
            status_code=403, content={"error": "Access forbidden"}
        )
    if conversation.deleted:
        return None, JSONResponse(
            status_code=409, content={"error": "Conversation is deleted"}
        )
    if conversation.audio_archived:
        return None, JSONResponse(
            status_code=409, content={"error": "Conversation audio is archived"}
        )
    if not conversation.audio_chunks_count:
        return None, JSONResponse(
            status_code=409, content={"error": "Conversation has no audio"}
        )
    if conversation.derived_into:
        return None, JSONResponse(
            status_code=409,
            content={"error": "Conversation was already split/merged"},
        )
    return conversation, None


async def _chunk_timeline(conversation_id: str) -> List[dict]:
    """Chunk metadata (no audio bytes) sorted by chunk_index."""
    collection = AudioChunkDocument.get_pymongo_collection()
    cursor = collection.find(
        {"conversation_id": conversation_id}, _CHUNK_META_PROJECTION
    ).sort("chunk_index", 1)
    return await cursor.to_list(length=None)


def _active_transcript_version(
    conversation: Conversation,
) -> Optional["Conversation.TranscriptVersion"]:
    for version in conversation.transcript_versions:
        if version.version_id == conversation.active_transcript_version:
            return version
    return None


async def _delete_source_memories(user_id: str, conversation_id: str) -> None:
    """Best-effort removal of memories sourced from a replaced conversation."""
    try:
        memory_service = get_memory_service()
        deleted = await memory_service.delete_memories_by_source(
            user_id, conversation_id
        )
        if deleted:
            logger.info(
                f"Deleted {deleted} memories sourced from {conversation_id[:12]}"
            )
    except Exception as e:
        logger.warning(
            f"Memory cleanup failed for {conversation_id[:12]} (non-fatal): {e}"
        )


async def get_silence_gaps(
    user: User,
    conversation_id: str,
    speech_threshold: float = 0.5,
    min_gap_seconds: float = 900.0,
):
    """Silence gaps (candidate split points) from cached chunk VAD scores."""
    conversation, error = await _load_operable_conversation(user, conversation_id)
    if error:
        return error

    chunks = await _chunk_timeline(conversation_id)
    if not chunks:
        return JSONResponse(
            status_code=409, content={"error": "Conversation has no audio chunks"}
        )

    needs_analysis = any((c.get("vad") or {}).get("max_score") is None for c in chunks)
    duration = float(chunks[-1]["end_time"])

    gaps = (
        []
        if needs_analysis
        else detect_silence_gaps(
            chunks,
            speech_threshold=speech_threshold,
            min_gap_seconds=min_gap_seconds,
        )
    )

    return {
        "analyzed": not needs_analysis,
        "needs_analysis": needs_analysis,
        "duration_seconds": round(duration, 2),
        "chunk_duration_seconds": float(chunks[0].get("duration") or 10.0),
        "speech_threshold": speech_threshold,
        "min_gap_seconds": min_gap_seconds,
        "gaps": gaps,
    }


async def get_speech_regions(
    user: User, conversation_id: str, speakers: Optional[List[str]] = None
):
    """Merged speech intervals for speech-skip playback.

    Without ``speakers``: served from the cached ``vad_analysis.speech_regions``
    when present; otherwise derived from the chunk-level frame scores (no audio
    decode) and cached back onto the conversation.

    With ``speakers``: the raw frame-level speech intervals are intersected
    with the selected speakers' transcript segments *before* merging, so the
    regions cover only time where the VAD heard voice while one of those
    speakers was tagged. Speaker labels match ``identified_as`` (recognized
    name) falling back to the raw ``speaker`` label, same as the audit
    listing. Filtered results are never cached (they depend on the selection).
    """
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        return JSONResponse(
            status_code=404, content={"error": "Conversation not found"}
        )
    if not user.is_superuser and conversation.user_id != str(user.user_id):
        return JSONResponse(status_code=403, content={"error": "Access forbidden"})
    if conversation.audio_archived or not conversation.audio_chunks_count:
        return JSONResponse(
            status_code=409, content={"error": "Conversation has no audio"}
        )

    wanted = {s for s in (speakers or []) if s}
    duration = conversation.audio_total_duration or 0.0
    va = conversation.vad_analysis

    if not wanted and va is not None and va.speech_regions is not None:
        regions = va.speech_regions
    else:
        # Derive from chunk frame scores with a streaming cursor (score
        # vectors are ~5KB per chunk; never materialize them all at once).
        collection = AudioChunkDocument.get_pymongo_collection()
        cursor = collection.find(
            {"conversation_id": conversation_id},
            {"start_time": 1, "end_time": 1, "vad.scores": 1, "vad.frame_hop_ms": 1},
        ).sort("chunk_index", 1)

        raw_intervals: List[List[float]] = []
        needs_analysis = False
        last_end = 0.0
        async for chunk in cursor:
            vad = chunk.get("vad")
            if not vad or vad.get("scores") is None:
                needs_analysis = True
                break
            raw_intervals.extend(
                frame_speech_intervals(
                    vad["scores"],
                    float(vad["frame_hop_ms"]) / 1000.0,
                    float(chunk["start_time"]),
                )
            )
            last_end = float(chunk["end_time"])

        if needs_analysis:
            return {
                "analyzed": False,
                "needs_analysis": True,
                "duration_seconds": round(duration, 2),
                "speech_seconds": 0,
                "regions": [],
            }

        duration = duration or last_end
        if wanted:
            version = _active_transcript_version(conversation)
            tagged = [
                [segment.start, segment.end]
                for segment in (version.segments if version else [])
                if segment.segment_type == Conversation.SegmentType.SPEECH
                and (segment.identified_as or segment.speaker) in wanted
            ]
            raw_intervals = intersect_intervals(raw_intervals, tagged)
        regions = merge_speech_regions(raw_intervals, duration)
        if not wanted and va is not None:
            va.speech_regions = regions
            await conversation.save()

    speech_seconds = sum(end - start for start, end in regions)
    return {
        "analyzed": True,
        "needs_analysis": False,
        "duration_seconds": round(duration, 2),
        "speech_seconds": round(speech_seconds, 2),
        "speakers": sorted(wanted),
        "regions": [{"start": start, "end": end} for start, end in regions],
    }


def _snap_split_points(
    split_points: List[float], chunks: List[dict]
) -> Tuple[Optional[List[int]], Optional[str]]:
    """Snap time points to chunk boundaries.

    Returns (sorted chunk indices that begin each new child, None) or
    (None, error message). Each child must contain at least one chunk, so
    snapped indices must be unique and within (0, n_chunks).
    """
    n_chunks = len(chunks)
    duration = float(chunks[-1]["end_time"])

    indices = []
    for point in sorted(set(split_points)):
        if not (0 < point < duration):
            return None, f"Split point {point}s is outside (0, {duration:.0f}s)"
        # The chunk containing the point begins the next child.
        snapped = None
        for chunk in chunks:
            if chunk["start_time"] <= point < chunk["end_time"]:
                snapped = int(chunk["chunk_index"])
                break
        if snapped is None:
            snapped = n_chunks - 1
        if snapped == 0:
            return None, f"Split point {point}s leaves an empty first part"
        indices.append(snapped)

    if len(set(indices)) != len(indices):
        return None, "Split points collapse onto the same chunk boundary"
    return indices, None


async def split_conversation(
    user: User, conversation_id: str, split_points: List[float]
):
    """Split a conversation into children at the given time points.

    Chunk documents are reassigned to the children (no audio re-encode); the
    parent's active transcript is sliced by time range; the parent is
    soft-deleted with lineage metadata; memory + title jobs run per child.
    Crash-safe ordering without transactions: children are created first, the
    parent is mutated last.
    """
    conversation, error = await _load_operable_conversation(user, conversation_id)
    if error:
        return error

    try:
        chunks = await _chunk_timeline(conversation_id)
        if not chunks:
            return JSONResponse(
                status_code=409, content={"error": "Conversation has no audio chunks"}
            )

        boundary_indices, message = _snap_split_points(split_points, chunks)
        if message:
            return JSONResponse(status_code=422, content={"error": message})

        # Build [start_index, end_index) chunk ranges for each child.
        edges = [0] + boundary_indices + [len(chunks)]
        chunk_by_index = {int(c["chunk_index"]): c for c in chunks}
        parent_version = _active_transcript_version(conversation)
        now = datetime.now(timezone.utc)

        children: List[Conversation] = []
        child_specs: List[dict] = []
        for a, b in zip(edges[:-1], edges[1:]):
            t0 = float(chunk_by_index[a]["start_time"])
            t1 = float(chunk_by_index[b - 1]["end_time"])

            child = create_conversation(
                user_id=conversation.user_id,
                client_id=conversation.client_id,
            )
            child.derived_from = Conversation.DerivedFrom(
                operation="split",
                source_conversation_ids=[conversation_id],
                time_range=[t0, t1],
                performed_at=now,
                performed_by=str(user.user_id),
            )
            child.end_reason = conversation.end_reason
            child.audio_chunks_count = b - a
            child.audio_total_duration = round(
                sum(
                    float(chunk_by_index[i].get("duration") or 0.0) for i in range(a, b)
                ),
                2,
            )
            child.audio_compression_ratio = conversation.audio_compression_ratio

            version_id = None
            if parent_version:
                segments = slice_segments(parent_version.segments or [], t0, t1)
                words = slice_words(parent_version.words or [], t0, t1)
                if segments or words:
                    version_id = str(uuid.uuid4())
                    child.add_transcript_version(
                        version_id=version_id,
                        transcript=build_transcript_text(segments),
                        words=words,
                        segments=segments,
                        provider=parent_version.provider,
                        model=parent_version.model,
                        metadata={
                            "derived": "split",
                            "source_conversation_id": conversation_id,
                            "source_version_id": parent_version.version_id,
                            "time_range": [t0, t1],
                        },
                        set_as_active=True,
                    )

            part = len(children) + 1
            total = len(edges) - 1
            base_title = conversation.title or conversation_id[:8]
            child.title = f"Part {part}/{total} — {base_title}"

            await child.insert()
            children.append(child)
            child_specs.append(
                {"a": a, "b": b, "t0": t0, "t1": t1, "version_id": version_id}
            )

        # Re-validate just before mutating chunks (no lock; admin tool).
        current = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if current is None or current.deleted or current.derived_into:
            for child in children:
                await child.delete()
            return JSONResponse(
                status_code=409,
                content={"error": "Conversation changed during split; aborted"},
            )

        # Move chunks to the children: re-id, re-index, shift times.
        collection = AudioChunkDocument.get_pymongo_collection()
        for child, spec in zip(children, child_specs):
            await collection.update_many(
                {
                    "conversation_id": conversation_id,
                    "chunk_index": {"$gte": spec["a"], "$lt": spec["b"]},
                },
                [
                    {
                        "$set": {
                            "conversation_id": child.conversation_id,
                            "chunk_index": {"$subtract": ["$chunk_index", spec["a"]]},
                            "start_time": {"$subtract": ["$start_time", spec["t0"]]},
                            "end_time": {"$subtract": ["$end_time", spec["t0"]]},
                        }
                    }
                ],
            )

        # Soft-delete the parent (chunks were moved, not deleted).
        conversation.deleted = True
        conversation.deletion_reason = "split"
        conversation.deleted_at = now
        conversation.derived_into = [c.conversation_id for c in children]
        conversation.audio_chunks_count = 0
        await conversation.save()

        await _delete_source_memories(conversation.user_id, conversation_id)

        results = []
        for child, spec in zip(children, child_specs):
            jobs = None
            if spec["version_id"]:
                jobs = start_post_conversation_jobs(
                    child.conversation_id,
                    conversation.user_id,
                    transcript_version_id=spec["version_id"],
                    client_id=conversation.client_id,
                    end_reason="split",
                    skip_speaker_recognition=True,
                )
            results.append(
                {
                    "conversation_id": child.conversation_id,
                    "start_seconds": spec["t0"],
                    "end_seconds": spec["t1"],
                    "duration_seconds": child.audio_total_duration,
                    "chunk_count": child.audio_chunks_count,
                    "has_transcript": spec["version_id"] is not None,
                    "jobs": jobs,
                }
            )

        logger.info(
            f"Split conversation {conversation_id[:12]} into {len(children)} parts"
        )
        return {
            "parent_conversation_id": conversation_id,
            "children": results,
        }

    except Exception as e:
        logger.exception(f"Error splitting conversation {conversation_id}: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error splitting conversation"}
        )


async def merge_conversations(user: User, conversation_ids: List[str]):
    """Merge adjacent conversations into a new conversation.

    Creates a fresh conversation (symmetric with split: sources stay intact
    and individually recoverable), reassigns chunks with cumulative time
    offsets, concatenates transcripts with a seam note where the wall-clock
    gap between recordings is elided, soft-deletes the sources, and enqueues
    memory + title jobs. Crash-safe ordering: the merged conversation is
    created before any source is mutated.
    """
    if len(set(conversation_ids)) != len(conversation_ids):
        return JSONResponse(
            status_code=422, content={"error": "Duplicate conversation ids"}
        )

    try:
        sources: List[Conversation] = []
        for cid in conversation_ids:
            conversation, error = await _load_operable_conversation(user, cid)
            if error:
                return error
            sources.append(conversation)

        client_ids = {s.client_id for s in sources}
        if len(client_ids) != 1:
            return JSONResponse(
                status_code=422,
                content={"error": "Conversations belong to different devices"},
            )
        user_ids = {s.user_id for s in sources}
        if len(user_ids) != 1:
            return JSONResponse(
                status_code=422,
                content={"error": "Conversations belong to different users"},
            )

        sources.sort(key=lambda s: s.created_at)
        first, last = sources[0], sources[-1]

        # Adjacency: no other live conversation of this device may sit between
        # the earliest and latest selected (server-authoritative check).
        conv_collection = Conversation.get_pymongo_collection()
        between = await conv_collection.count_documents(
            {
                "client_id": first.client_id,
                "deleted": {"$ne": True},
                "conversation_id": {"$nin": conversation_ids},
                "created_at": {"$gt": first.created_at, "$lt": last.created_at},
            }
        )
        if between:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "Conversations are not adjacent: "
                    f"{between} other conversation(s) lie between them"
                },
            )

        # Uniform audio format across all sources' chunks.
        chunk_collection = AudioChunkDocument.get_pymongo_collection()
        chunk_filter = {"conversation_id": {"$in": conversation_ids}}
        sample_rates = await chunk_collection.distinct("sample_rate", chunk_filter)
        channel_counts = await chunk_collection.distinct("channels", chunk_filter)
        if len(sample_rates) > 1 or len(channel_counts) > 1:
            return JSONResponse(
                status_code=422,
                content={"error": "Conversations have mixed audio formats"},
            )

        # Precise per-source duration/count from the chunks themselves.
        stats = {
            row["_id"]: row
            for row in await chunk_collection.aggregate(
                [
                    {"$match": chunk_filter},
                    {
                        "$group": {
                            "_id": "$conversation_id",
                            "duration": {"$sum": "$duration"},
                            "count": {"$sum": 1},
                        }
                    },
                ]
            ).to_list(length=None)
        }
        missing = [s.conversation_id for s in sources if s.conversation_id not in stats]
        if missing:
            return JSONResponse(
                status_code=409,
                content={"error": f"No audio chunks for {missing[0]}"},
            )

        now = datetime.now(timezone.utc)
        merged = create_conversation(user_id=first.user_id, client_id=first.client_id)
        merged.created_at = first.created_at
        merged.derived_from = Conversation.DerivedFrom(
            operation="merge",
            source_conversation_ids=[s.conversation_id for s in sources],
            performed_at=now,
            performed_by=str(user.user_id),
        )
        merged.end_reason = Conversation.EndReason.MERGE
        merged.audio_chunks_count = sum(
            int(stats[s.conversation_id]["count"]) for s in sources
        )
        merged.audio_total_duration = round(
            sum(float(stats[s.conversation_id]["duration"]) for s in sources), 2
        )
        merged.audio_compression_ratio = first.audio_compression_ratio

        # Concatenate transcripts with cumulative offsets + seam notes.
        merged_segments: List[Conversation.SpeakerSegment] = []
        merged_words: List[Conversation.Word] = []
        provider = None
        model = None
        offset = 0.0
        prev = None
        for source in sources:
            version = _active_transcript_version(source)
            if prev is not None:
                gap_seconds = max(
                    0.0,
                    (source.created_at - prev.created_at).total_seconds()
                    - float(stats[prev.conversation_id]["duration"]),
                )
                merged_segments.append(
                    Conversation.SpeakerSegment(
                        start=round(offset, 3),
                        end=round(offset, 3),
                        text=(
                            f"[merged: {max(1, round(gap_seconds / 60))} min gap "
                            "between recordings elided]"
                        ),
                        speaker="system",
                        segment_type=Conversation.SegmentType.NOTE,
                    )
                )
            if version:
                merged_segments.extend(shift_segments(version.segments or [], offset))
                merged_words.extend(shift_words(version.words or [], offset))
                provider = provider or version.provider
                model = model or version.model
            offset += float(stats[source.conversation_id]["duration"])
            prev = source

        version_id = None
        has_content = any(
            seg.segment_type == Conversation.SegmentType.SPEECH
            for seg in merged_segments
        ) or bool(merged_words)
        if has_content:
            version_id = str(uuid.uuid4())
            merged.add_transcript_version(
                version_id=version_id,
                transcript=build_transcript_text(merged_segments),
                words=merged_words,
                segments=merged_segments,
                provider=provider,
                model=model,
                metadata={
                    "derived": "merge",
                    "source_conversation_ids": [s.conversation_id for s in sources],
                },
                set_as_active=True,
            )

        merged.title = first.title or f"Merged conversation ({len(sources)} parts)"
        await merged.insert()

        # Move chunks: per source, offset index/time and re-id.
        offset = 0.0
        index_base = 0
        for source in sources:
            await chunk_collection.update_many(
                {"conversation_id": source.conversation_id},
                [
                    {
                        "$set": {
                            "conversation_id": merged.conversation_id,
                            "chunk_index": {"$add": ["$chunk_index", index_base]},
                            "start_time": {"$add": ["$start_time", offset]},
                            "end_time": {"$add": ["$end_time", offset]},
                        }
                    }
                ],
            )
            offset += float(stats[source.conversation_id]["duration"])
            index_base += int(stats[source.conversation_id]["count"])

        # Soft-delete sources with lineage (chunks were moved, not deleted).
        for source in sources:
            source.deleted = True
            source.deletion_reason = "merged"
            source.deleted_at = now
            source.derived_into = [merged.conversation_id]
            source.audio_chunks_count = 0
            await source.save()
            await _delete_source_memories(source.user_id, source.conversation_id)

        jobs = None
        if version_id:
            jobs = start_post_conversation_jobs(
                merged.conversation_id,
                merged.user_id,
                transcript_version_id=version_id,
                client_id=merged.client_id,
                end_reason="merge",
                skip_speaker_recognition=True,
            )

        logger.info(
            f"Merged {len(sources)} conversations into {merged.conversation_id[:12]}"
        )
        return {
            "merged_conversation_id": merged.conversation_id,
            "source_conversation_ids": [s.conversation_id for s in sources],
            "duration_seconds": merged.audio_total_duration,
            "chunk_count": merged.audio_chunks_count,
            "has_transcript": version_id is not None,
            "jobs": jobs,
        }

    except Exception as e:
        logger.exception(f"Error merging conversations {conversation_ids}: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error merging conversations"}
        )


# ---------------------------------------------------------------------------
# Annotation dataset export
# ---------------------------------------------------------------------------


async def start_screening(
    user: User,
    conversation_ids: List[str],
    policy: Optional[str] = None,
):
    """Enqueue the privacy-screen job for selected conversations.

    The job applies the shareability ``policy`` (or the configured default) to
    each conversation's transcript and returns the flagged segments for the
    user to review before exporting.
    """
    try:
        job = default_queue.enqueue(
            screen_conversations_job,
            user_id=str(user.user_id),
            conversation_ids=conversation_ids,
            policy=policy,
            job_timeout=1800,
            result_ttl=JOB_RESULT_TTL,
            description=f"Privacy-screen {len(conversation_ids)} conversations",
        )
        logger.info(
            f"Enqueued sensitivity screen (job {job.id}) for user {user.user_id}"
        )
        return {"job_id": job.id, "status": "queued"}
    except Exception as e:
        logger.exception(f"Error enqueueing sensitivity screen: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error enqueueing screen"}
        )


async def get_default_sensitivity_policy():
    """Return the configured default shareability policy (UI prefill)."""
    return {"policy": get_sensitivity_policy()}


async def start_export(
    user: User,
    conversation_ids: List[str],
    mode: str = "clips",
    pad_seconds: float = 1.0,
    speech_threshold: float = 0.5,
    merge_gap_seconds: float = 3.0,
    excluded_ranges: Optional[Dict[str, List[List[float]]]] = None,
    sensitivity_policy: Optional[str] = None,
):
    """Enqueue the annotation-dataset export job for selected conversations.

    ``excluded_ranges`` maps conversation_id → withheld ``[start, end]`` ranges
    confirmed from the privacy screen; those are carved out of the export.
    """
    try:
        export_id = new_export_id()
        job = default_queue.enqueue(
            export_annotation_dataset_job,
            user_id=str(user.user_id),
            export_id=export_id,
            conversation_ids=conversation_ids,
            mode=mode,
            pad_seconds=pad_seconds,
            speech_threshold=speech_threshold,
            merge_gap_seconds=merge_gap_seconds,
            excluded_ranges=excluded_ranges,
            sensitivity_policy=sensitivity_policy,
            job_timeout=3600,
            result_ttl=JOB_RESULT_TTL,
            description=f"Export annotation dataset ({len(conversation_ids)} conversations, {mode})",
        )
        logger.info(
            f"Enqueued annotation export {export_id} (job {job.id}) "
            f"for user {user.user_id}"
        )
        return {"job_id": job.id, "export_id": export_id, "status": "queued"}
    except Exception as e:
        logger.exception(f"Error enqueueing annotation export: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error enqueueing export"}
        )


def _read_export_meta(user: User, export_id: str):
    """Load an export's metadata, enforcing id validity + ownership.

    Returns (meta_dict, None) or (None, error_response).
    """
    if not validate_export_id(export_id):
        return None, JSONResponse(
            status_code=422, content={"error": "Invalid export id"}
        )
    meta_path = export_dir(export_id) / META_NAME
    if not meta_path.is_file():
        return None, JSONResponse(
            status_code=404, content={"error": "Export not found"}
        )
    meta = json.loads(meta_path.read_text())
    if not user.is_superuser and meta.get("created_by") != str(user.user_id):
        return None, JSONResponse(
            status_code=403, content={"error": "Access forbidden"}
        )
    return meta, None


async def list_exports(user: User):
    """List completed exports (superusers see all, others their own)."""
    exports = []
    if EXPORTS_DIR.is_dir():
        for meta_path in EXPORTS_DIR.glob(f"*/{META_NAME}"):
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                logger.warning(f"Unreadable export metadata: {meta_path}")
                continue
            if not user.is_superuser and meta.get("created_by") != str(user.user_id):
                continue
            meta["zip_ready"] = (meta_path.parent / ZIP_NAME).is_file()
            exports.append(meta)
    exports.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return {"exports": exports}


async def download_export(user: User, export_id: str):
    """Stream an export's dataset.zip as an attachment."""
    meta, error = _read_export_meta(user, export_id)
    if error:
        return error
    zip_path = export_dir(export_id) / ZIP_NAME
    if not zip_path.is_file():
        return JSONResponse(
            status_code=404, content={"error": "Export zip not found (job failed?)"}
        )
    return FileResponse(
        zip_path, media_type="application/zip", filename=f"{export_id}.zip"
    )


async def delete_export(user: User, export_id: str):
    """Delete an export directory (zip + metadata)."""
    meta, error = _read_export_meta(user, export_id)
    if error:
        return error
    shutil.rmtree(export_dir(export_id))
    logger.info(f"Deleted annotation export {export_id}")
    return {"deleted": True, "export_id": export_id}
