"""
Data-cleaning controller.

Backs the Data Cleaning dashboard page: surfaces per-conversation amplitude
metrics + latest speaker labels, filters by a compound predicate (silence
thresholds AND speaker include/exclude), enqueues batch audio analysis, and
archives (hard-deletes) audio for selected conversations.
"""

import json
import logging
from typing import List, Optional

from fastapi.responses import JSONResponse

from advanced_omi_backend.controllers.conversation_controller import (
    archive_conversation_audio,
)
from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    default_queue,
)
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.users import User
from advanced_omi_backend.utils.silence_analysis import silent_fraction_from_histogram
from advanced_omi_backend.workers.data_cleaning_jobs import (
    analyze_silence_batch_job,
    auto_clean_job,
)

logger = logging.getLogger(__name__)

# Upper bound on how many conversations a single scan inspects in memory.
# Speaker/silence predicates are applied in Python, so we cap the working set
# and report when it was hit rather than silently truncating.
MAX_SCAN = 2000

# Projection: lightweight metadata + cached silence analysis + speaker labels
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
    "silence_analysis": 1,
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


async def list_for_cleaning(
    user: User,
    silence_threshold_dbfs: float = -45.0,
    min_silent_fraction: float = 0.0,
    min_duration: float = 0.0,
    include_speakers: Optional[List[str]] = None,
    exclude_speakers: Optional[List[str]] = None,
    archived_only: bool = False,
    limit: int = 100,
    offset: int = 0,
):
    """List conversations with amplitude metrics + speakers, filtered by the
    compound predicate. Returns derived ``silent_fraction`` at the requested
    threshold so the UI threshold slider is just a re-fetch.

    Speaker filtering is per-speaker tri-state: a conversation is kept only if
    it contains at least one ``include_speakers`` (when any are set) AND none of
    the ``exclude_speakers``.
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

        for doc in raw_docs:
            duration = doc.get("audio_total_duration") or 0.0
            sa = doc.get("silence_analysis")
            doc_speakers = _speakers_for_doc(doc)

            silent_fraction = None
            mean_dbfs = None
            peak_dbfs = None
            if sa:
                mean_dbfs = sa.get("mean_dbfs")
                peak_dbfs = sa.get("peak_dbfs")
                silent_fraction = silent_fraction_from_histogram(
                    histogram=sa.get("histogram") or [],
                    window_count=sa.get("window_count") or 0,
                    histogram_min_dbfs=sa.get("histogram_min_dbfs"),
                    histogram_bin_width=sa.get("histogram_bin_width"),
                    threshold_dbfs=silence_threshold_dbfs,
                )

            # --- Compound filter (skip filters for the archived audit view) ---
            if not archived_only:
                if duration < min_duration:
                    continue
                if min_silent_fraction > 0:
                    # Unanalyzed conversations can't satisfy a silence filter
                    if silent_fraction is None or silent_fraction < min_silent_fraction:
                        continue
                if include_set and not include_set.intersection(doc_speakers):
                    continue
                if exclude_set and exclude_set.intersection(doc_speakers):
                    continue

            created_at = doc.get("created_at")
            archived_at = doc.get("audio_archived_at")
            matched.append(
                {
                    "conversation_id": doc.get("conversation_id"),
                    "title": doc.get("title"),
                    "client_id": doc.get("client_id"),
                    "created_at": created_at.isoformat() if created_at else None,
                    "duration_seconds": duration,
                    "speakers": doc_speakers,
                    "analyzed": sa is not None,
                    "silent_fraction": (
                        round(silent_fraction, 4)
                        if silent_fraction is not None
                        else None
                    ),
                    "mean_dbfs": mean_dbfs,
                    "peak_dbfs": peak_dbfs,
                    "audio_archived": doc.get("audio_archived", False),
                    "audio_archived_at": (
                        archived_at.isoformat() if archived_at else None
                    ),
                    "archive_reason": doc.get("archive_reason"),
                }
            )

        total = len(matched)
        page = matched[offset : offset + limit]

        return {
            "conversations": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "scan_capped": scan_capped,
            "silence_threshold_dbfs": silence_threshold_dbfs,
        }

    except Exception as e:
        logger.exception(f"Error listing conversations for cleaning: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error listing conversations"}
        )


async def list_speakers(user: User):
    """Distinct speaker labels across the user's (non-archived) conversations."""
    try:
        base: dict = {} if user.is_superuser else {"user_id": str(user.user_id)}
        base["audio_archived"] = {"$ne": True}
        base["deleted"] = {"$ne": True}

        collection = Conversation.get_pymongo_collection()
        cursor = collection.find(
            base,
            {
                "active_transcript_version": 1,
                "transcript_versions.version_id": 1,
                "transcript_versions.segments.speaker": 1,
                "transcript_versions.segments.identified_as": 1,
            },
        ).limit(MAX_SCAN)
        raw_docs = await cursor.to_list(length=MAX_SCAN)

        speakers: set = set()
        for doc in raw_docs:
            speakers.update(_speakers_for_doc(doc))

        return {"speakers": sorted(speakers)}

    except Exception as e:
        logger.exception(f"Error listing speakers: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error listing speakers"}
        )


async def enqueue_analysis(
    user: User, conversation_ids: Optional[List[str]] = None, force: bool = False
):
    """Enqueue the batch silence-analysis job for the user's conversations."""
    try:
        # Pass as kwargs so the job's user_id is recorded in job.kwargs — the
        # queue status endpoint authorizes non-admins by job.kwargs["user_id"].
        job = default_queue.enqueue(
            analyze_silence_batch_job,
            user_id=str(user.user_id),
            conversation_ids=conversation_ids,
            force=force,
            job_timeout=3600,
            result_ttl=JOB_RESULT_TTL,
            description="Analyze conversation audio silence",
        )
        logger.info(f"Enqueued silence analysis job {job.id} for user {user.user_id}")
        return {"job_id": job.id, "status": "queued"}

    except Exception as e:
        logger.exception(f"Error enqueueing silence analysis: {e}")
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
        description="Auto-clean: archive near-silent conversations",
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
