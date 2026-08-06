"""
Conversation controller for handling conversation-related business logic.
"""

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import JSONResponse

from advanced_omi_backend.client_manager import (
    client_belongs_to_user,
    get_client_manager,
)
from advanced_omi_backend.config_loader import get_service_config
from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    conversation_edit_chain_in_flight,
    default_queue,
    memory_queue,
    post_conv_enqueue_kwargs,
    start_post_conversation_jobs,
    transcription_queue,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import JobPriority
from advanced_omi_backend.models.memory_audit import MemoryAuditEntry
from advanced_omi_backend.models.waveform import WaveformData
from advanced_omi_backend.plugins.events import ConversationCloseReason, PluginEvent
from advanced_omi_backend.redis_factory import create_async_redis
from advanced_omi_backend.services.audio_stream.session_store import SessionStore
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.services.memory.audit import (
    MemoryCause,
    UpdateStrategy,
    actor_for,
    source_kind_for,
    source_label_for,
)
from advanced_omi_backend.services.plugin_service import get_plugin_router
from advanced_omi_backend.users import User
from advanced_omi_backend.workers.conversation_jobs import generate_title_summary_job
from advanced_omi_backend.workers.memory_jobs import (
    enqueue_memory_processing,
    process_memory_job,
)
from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job
from advanced_omi_backend.workers.transcription_jobs import transcribe_full_audio_job

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")


async def _get_conversation_or_error(conversation_id: str, user: User):
    """Fetch a conversation and validate user access.

    Returns (conversation, None) on success, or (None, error_response) on failure.
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
    return conversation, None


async def close_current_conversation(client_id: str, user: User):
    """Close the current conversation for a specific client.

    Signals the open_conversation_job to close the current conversation
    and trigger post-processing. The session stays active for new conversations.
    """
    # Validate client ownership
    if not user.is_superuser and not client_belongs_to_user(client_id, user.user_id):
        logger.warning(
            f"User {user.user_id} attempted to close conversation for client {client_id} without permission"
        )
        return JSONResponse(
            content={
                "error": "Access forbidden. You can only close your own conversations.",
                "details": f"Client '{client_id}' does not belong to your account.",
            },
            status_code=403,
        )

    client_manager = get_client_manager()
    client_state = client_manager.get_client(client_id)
    if client_state is None or not client_state.connected:
        return JSONResponse(
            content={"error": f"Client '{client_id}' not found or not connected"},
            status_code=404,
        )

    session_id = client_state.stream_session_id
    if not session_id:
        return JSONResponse(
            content={"error": "No active session"},
            status_code=400,
        )

    # Signal the conversation job to close and trigger post-processing
    r = create_async_redis()
    try:
        success = await SessionStore(r).request_close(
            session_id, ConversationCloseReason.USER_REQUESTED.value
        )
    finally:
        await r.aclose()

    if not success:
        return JSONResponse(
            content={"error": "Session not found in Redis"},
            status_code=404,
        )

    logger.info(
        f"Conversation close requested for client {client_id} by user {user.user_id}"
    )

    return JSONResponse(
        content={
            "message": f"Conversation close requested for client '{client_id}'",
            "client_id": client_id,
            "timestamp": int(time.time()),
        }
    )


async def get_conversation(conversation_id: str, user: User):
    """Get a single conversation with full transcript details."""
    try:
        conversation, error = await _get_conversation_or_error(conversation_id, user)
        if error:
            return error
        active_version = conversation.active_transcript

        # Build response with explicit curated fields
        response = {
            "conversation_id": conversation.conversation_id,
            "user_id": conversation.user_id,
            "client_id": conversation.client_id,
            "audio_chunks_count": conversation.audio_chunks_count,
            "audio_total_duration": conversation.audio_total_duration,
            "audio_compression_ratio": conversation.audio_compression_ratio,
            "created_at": (
                conversation.created_at.isoformat() if conversation.created_at else None
            ),
            "deleted": conversation.deleted,
            "deletion_reason": conversation.deletion_reason,
            "deleted_at": (
                conversation.deleted_at.isoformat() if conversation.deleted_at else None
            ),
            "processing_status": conversation.processing_status,
            "failure_stage": conversation.failure_stage,
            "always_persist": conversation.always_persist,
            "end_reason": (
                conversation.end_reason.value if conversation.end_reason else None
            ),
            "completed_at": (
                conversation.completed_at.isoformat()
                if conversation.completed_at
                else None
            ),
            "title": conversation.title,
            "summary": conversation.summary,
            "detailed_summary": conversation.detailed_summary,
            # Computed fields
            "transcript": conversation.transcript,
            "segments": [s.model_dump() for s in conversation.segments],
            "segment_count": conversation.segment_count,
            "active_transcript_version": conversation.active_transcript_version,
            "transcript_version_count": conversation.transcript_version_count,
            "active_transcript_version_number": conversation.active_transcript_version_number,
            "speaker_recognition": (
                active_version.metadata.get("speaker_recognition")
                if active_version and active_version.metadata
                else None
            ),
            "diarization_source": (
                active_version.diarization_source if active_version else None
            ),
            "starred": conversation.starred,
            "starred_at": (
                conversation.starred_at.isoformat() if conversation.starred_at else None
            ),
        }

        return {"conversation": response}

    except Exception as e:
        logger.error(f"Error fetching conversation {conversation_id}: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error fetching conversation"}
        )


async def get_conversation_memories(conversation_id: str, user: User, limit: int = 100):
    """Get memories extracted from a specific conversation."""
    try:
        conversation, error = await _get_conversation_or_error(conversation_id, user)
        if error:
            return error

        memory_service = get_memory_service()
        memories = await memory_service.get_memories_by_source(
            user_id=str(user.user_id), source_id=conversation_id, limit=limit
        )

        return {
            "conversation_id": conversation_id,
            "memories": [mem.to_dict() for mem in memories],
            "count": len(memories),
        }

    except Exception as e:
        logger.error(f"Error fetching memories for conversation {conversation_id}: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error fetching conversation memories"}
        )


def _conversation_to_list_dict(conv: Conversation) -> dict:
    """Convert a Conversation model to a dict for list-view responses."""
    return {
        "conversation_id": conv.conversation_id,
        "user_id": conv.user_id,
        "client_id": conv.client_id,
        "audio_chunks_count": conv.audio_chunks_count,
        "audio_total_duration": conv.audio_total_duration,
        "duration_seconds": conv.audio_total_duration,
        "audio_compression_ratio": conv.audio_compression_ratio,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "deleted": conv.deleted,
        "deletion_reason": conv.deletion_reason,
        "deleted_at": conv.deleted_at.isoformat() if conv.deleted_at else None,
        "audio_archived": conv.audio_archived,
        "archive_reason": conv.archive_reason,
        "processing_status": conv.processing_status,
        "failure_stage": conv.failure_stage,
        "always_persist": conv.always_persist,
        "title": conv.title,
        "summary": conv.summary,
        "detailed_summary": conv.detailed_summary,
        "active_transcript_version": conv.active_transcript_version,
        "segment_count": conv.segment_count,
        "transcript_version_count": conv.transcript_version_count,
        "active_transcript_version_number": conv.active_transcript_version_number,
        "starred": conv.starred,
        "starred_at": conv.starred_at.isoformat() if conv.starred_at else None,
    }


def _raw_doc_to_list_dict(doc: dict) -> dict:
    """Convert a raw pymongo document (projected) to a list-view dict.

    Computes segment_count and the active version number from the lightweight
    projected version arrays without loading full transcript/word data.
    """
    active_tv = doc.get("active_transcript_version")

    # Compute segment_count + the unique speaker list from the active version's segments
    # (segments are already projected for the count — deriving speakers here is free and
    # lets the list show "who's in this conversation" at a glance without expanding).
    segment_count = 0
    speakers: list[str] = []
    transcript_versions = doc.get("transcript_versions") or []
    for tv in transcript_versions:
        if tv.get("version_id") == active_tv:
            segs = tv.get("segments", [])
            segment_count = len(segs)
            seen: set[str] = set()
            for seg in segs:
                sp = (seg.get("speaker") or "").strip()
                if sp and sp not in seen:
                    seen.add(sp)
                    speakers.append(sp)
            break

    # Compute active version number (1-based)
    active_transcript_version_number = None
    for i, tv in enumerate(transcript_versions):
        if tv.get("version_id") == active_tv:
            active_transcript_version_number = i + 1
            break

    created_at = doc.get("created_at")
    deleted_at = doc.get("deleted_at")
    starred_at = doc.get("starred_at")

    return {
        "conversation_id": doc.get("conversation_id"),
        "user_id": doc.get("user_id"),
        "client_id": doc.get("client_id"),
        "audio_chunks_count": doc.get("audio_chunks_count"),
        "audio_total_duration": doc.get("audio_total_duration"),
        "duration_seconds": doc.get("audio_total_duration"),
        "audio_compression_ratio": doc.get("audio_compression_ratio"),
        "created_at": created_at.isoformat() if created_at else None,
        "deleted": doc.get("deleted", False),
        "deletion_reason": doc.get("deletion_reason"),
        "deleted_at": deleted_at.isoformat() if deleted_at else None,
        "audio_archived": doc.get("audio_archived", False),
        "archive_reason": doc.get("archive_reason"),
        "processing_status": doc.get("processing_status"),
        "failure_stage": doc.get("failure_stage"),
        "always_persist": doc.get("always_persist", False),
        "title": doc.get("title"),
        "summary": doc.get("summary"),
        "detailed_summary": doc.get("detailed_summary"),
        "active_transcript_version": active_tv,
        "segment_count": segment_count,
        "speakers": speakers,
        "transcript_version_count": len(transcript_versions),
        "active_transcript_version_number": active_transcript_version_number,
        "starred": doc.get("starred", False),
        "starred_at": starred_at.isoformat() if starred_at else None,
    }


# Projection for list view — excludes heavy transcript/word data
_LIST_PROJECTION = {
    "conversation_id": 1,
    "user_id": 1,
    "client_id": 1,
    "audio_chunks_count": 1,
    "audio_total_duration": 1,
    "audio_compression_ratio": 1,
    "created_at": 1,
    "deleted": 1,
    "deletion_reason": 1,
    "deleted_at": 1,
    "audio_archived": 1,
    "archive_reason": 1,
    "processing_status": 1,
    "failure_stage": 1,
    "always_persist": 1,
    "title": 1,
    "summary": 1,
    "detailed_summary": 1,
    "starred": 1,
    "starred_at": 1,
    "active_transcript_version": 1,
    # Lightweight version metadata. Full segments (including text and word-level
    # timestamps) are loaded by the detail endpoint only when a transcript is opened.
    "transcript_versions.version_id": 1,
    "transcript_versions.segments.speaker": 1,
}


ALLOWED_SORT_FIELDS = {"created_at", "title", "audio_total_duration"}


async def get_conversations(
    user: User,
    include_deleted: bool = False,
    include_unprocessed: bool = False,
    starred_only: bool = False,
    limit: int = 200,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
):
    """Get conversations with speech only (speech-driven architecture).

    Uses a single consolidated query with ``$or`` when ``include_unprocessed``
    is True, eliminating multiple round-trips and Python-side merge/sort.
    Results are paginated with ``limit``/``offset``.
    """
    try:
        user_filter = {} if user.is_superuser else {"user_id": str(user.user_id)}
        # Continuous ScreenPipe artifacts are evidence for semantic episodes, not
        # user-facing conversations. The raw device-input API remains available.
        user_filter["external_source_type"] = {"$ne": "screenpipe"}

        if starred_only:
            user_filter["starred"] = True

        # Build query conditions — single $or when orphans are requested
        conditions = []

        # Condition 1: normal (non-deleted or all) conversations
        if include_deleted:
            conditions.append({})  # no filter on deleted
        else:
            conditions.append({"deleted": False})

        if include_unprocessed:
            # Orphan type 1: always_persist that ended up failed (not deleted).
            # "active" = still in-flight (don't flag); stale-active crashes are
            # reconciled to "failed" by the reconciler, so they show up here too.
            conditions.append(
                {
                    "always_persist": True,
                    "processing_status": Conversation.ConversationStatus.FAILED.value,
                    "deleted": False,
                }
            )
            # Orphan type 2: soft-deleted due to no speech but have audio data
            conditions.append(
                {
                    "deleted": True,
                    "deletion_reason": {
                        "$in": [
                            "no_meaningful_speech",
                            "audio_file_not_ready",
                            "no_meaningful_speech_batch_transcription",
                        ]
                    },
                    "audio_chunks_count": {"$gt": 0},
                }
            )

        # Assemble final query
        if len(conditions) == 1:
            query = {**user_filter, **conditions[0]}
        else:
            query = {**user_filter, "$or": conditions}

        # Validate and build sort
        if sort_by not in ALLOWED_SORT_FIELDS:
            sort_by = "created_at"
        sort_direction = 1 if sort_order == "asc" else -1

        collection = Conversation.get_pymongo_collection()

        total = await collection.count_documents(query)

        cursor = collection.find(query, _LIST_PROJECTION)
        cursor = cursor.sort(sort_by, sort_direction).skip(offset).limit(limit)
        raw_docs = await cursor.to_list(length=limit)

        # Mark orphans in results (lightweight in-memory check on the page)
        orphan_ids: set = set()
        if include_unprocessed:
            for doc in raw_docs:
                conv_id = doc.get("conversation_id")
                is_orphan_type1 = (
                    doc.get("always_persist")
                    and doc.get("processing_status")
                    == Conversation.ConversationStatus.FAILED.value
                    and not doc.get("deleted")
                )
                is_orphan_type2 = (
                    doc.get("deleted")
                    and doc.get("deletion_reason")
                    in (
                        "no_meaningful_speech",
                        "audio_file_not_ready",
                        "no_meaningful_speech_batch_transcription",
                    )
                    and (doc.get("audio_chunks_count") or 0) > 0
                )
                if is_orphan_type1 or is_orphan_type2:
                    orphan_ids.add(conv_id)

        # Build response from projected documents - no Beanie model overhead
        conversations = []
        for doc in raw_docs:
            d = _raw_doc_to_list_dict(doc)
            d["is_orphan"] = doc.get("conversation_id") in orphan_ids
            conversations.append(d)

        return {
            "conversations": conversations,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    except Exception as e:
        logger.exception(f"Error fetching conversations: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error fetching conversations"}
        )


# MongoDB fields covered by each independently selectable search category.
_SEARCH_CATEGORY_FIELDS: dict[str, list[str]] = {
    "id": ["conversation_id"],
    "title": ["title"],
    "summary": ["summary", "detailed_summary"],
    "speakers": ["_search_active_version.segments.speaker"],
}


def _search_fields(categories: list[str]) -> list[str]:
    return [
        field
        for category in categories
        for field in _SEARCH_CATEGORY_FIELDS.get(category, [])
    ]


def _search_query_stages(query: str, fields: list[str]) -> list[dict]:
    """Build query stages, resolving speaker labels from the active version only."""
    if not query:
        return []

    stages: list[dict] = []
    if any(field.startswith("_search_active_version.") for field in fields):
        stages.append(
            {
                "$set": {
                    "_search_active_version": {
                        "$arrayElemAt": [
                            {
                                "$filter": {
                                    "input": {"$ifNull": ["$transcript_versions", []]},
                                    "as": "version",
                                    "cond": {
                                        "$eq": [
                                            "$$version.version_id",
                                            "$active_transcript_version",
                                        ]
                                    },
                                }
                            },
                            0,
                        ]
                    }
                }
            }
        )

    regex = {"$regex": re.escape(query), "$options": "i"}
    stages.append({"$match": {"$or": [{field: regex} for field in fields]}})
    return stages


async def _regex_search_conversations(
    query: str,
    user: User,
    fields: list[str],
    limit: int,
    offset: int,
):
    """Filter conversations by text across the selected conversation fields."""
    collection = Conversation.get_pymongo_collection()

    match_filter: dict = {
        "deleted": False,
        "external_source_type": {"$ne": "screenpipe"},
    }
    if not user.is_superuser:
        match_filter["user_id"] = str(user.user_id)

    pipeline: list[dict] = [{"$match": match_filter}]
    pipeline.extend(_search_query_stages(query, fields))

    pipeline.extend(
        [
            {"$sort": {"created_at": -1}},
            {
                "$facet": {
                    "results": [
                        {"$skip": offset},
                        {"$limit": limit},
                        {"$project": _LIST_PROJECTION},
                    ],
                    "count": [{"$count": "total"}],
                }
            },
        ]
    )

    cursor = collection.aggregate(pipeline)
    facet_result = await cursor.to_list(length=1)
    facet = facet_result[0] if facet_result else {"results": [], "count": []}

    raw_docs = facet.get("results", [])
    count_list = facet.get("count", [])
    total = count_list[0]["total"] if count_list else 0

    conversations = []
    for doc in raw_docs:
        d = _raw_doc_to_list_dict(doc)
        d["is_orphan"] = False
        conversations.append(d)

    return {"conversations": conversations, "total": total}


async def search_conversations(
    query: str,
    user: User,
    limit: int = 50,
    offset: int = 0,
    categories: list[str] | None = None,
):
    """Search conversations by literal pattern across selected field categories."""
    categories = categories or ["id", "title", "summary", "speakers"]
    fields = _search_fields(categories)
    try:
        result = await _regex_search_conversations(query, user, fields, limit, offset)
        return {
            **result,
            "limit": limit,
            "offset": offset,
            "query": query,
            "fields": categories,
        }

    except Exception as e:
        logger.exception(f"Error searching conversations: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error searching conversations"}
        )


async def _soft_delete_conversation(
    conversation: Conversation, user: User
) -> JSONResponse:
    """Mark conversation and chunks as deleted (soft delete).

    Chunks are soft-deleted first so that a crash between the two writes
    leaves chunks deleted but the conversation still active — a safe state
    where a retry will complete the operation.
    """
    conversation_id = conversation.conversation_id
    deleted_at = datetime.now(timezone.utc)

    # 1. Soft delete audio chunks FIRST (safe failure mode: orphaned-deleted chunks)
    result = await AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conversation_id,
        AudioChunkDocument.deleted == False,
    ).update_many({"$set": {"deleted": True, "deleted_at": deleted_at}})

    deleted_chunks = result.modified_count
    logger.info(
        f"Soft deleted {deleted_chunks} audio chunks for conversation {conversation_id}"
    )

    # 2. Mark conversation as deleted
    conversation.deleted = True
    conversation.deletion_reason = "user_deleted"
    conversation.deleted_at = deleted_at
    try:
        await conversation.save()
    except Exception:
        # Rollback: undo chunk soft-delete using the exact timestamp we set
        logger.error(
            f"Failed to soft-delete conversation {conversation_id}, rolling back chunk deletes"
        )
        await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id,
            AudioChunkDocument.deleted_at == deleted_at,
        ).update_many({"$set": {"deleted": False, "deleted_at": None}})
        raise

    logger.info(f"Soft deleted conversation {conversation_id} for user {user.user_id}")

    return JSONResponse(
        status_code=200,
        content={
            "message": f"Successfully soft deleted conversation '{conversation_id}'",
            "deleted_chunks": deleted_chunks,
            "conversation_id": conversation_id,
            "client_id": conversation.client_id,
            "deleted_at": (
                conversation.deleted_at.isoformat() if conversation.deleted_at else None
            ),
        },
    )


async def _hard_delete_conversation(conversation: Conversation) -> JSONResponse:
    """Permanently delete conversation and chunks (admin only).

    Chunks are deleted first so that a crash between the two writes
    leaves the conversation document intact — an admin can retry the
    delete since the conversation still exists.
    """
    conversation_id = conversation.conversation_id
    client_id = conversation.client_id

    # 1. Delete audio chunks FIRST (no rollback possible for hard deletes)
    result = await AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conversation_id
    ).delete()

    deleted_chunks = result.deleted_count
    logger.info(
        f"Hard deleted {deleted_chunks} audio chunks for conversation {conversation_id}"
    )

    # 2. Delete conversation document
    try:
        await conversation.delete()
    except Exception:
        logger.error(
            f"Failed to hard-delete conversation {conversation_id} after "
            f"deleting {deleted_chunks} chunks. Conversation document remains — retry delete."
        )
        raise

    logger.info(f"Hard deleted conversation {conversation_id}")

    return JSONResponse(
        status_code=200,
        content={
            "message": f"Successfully permanently deleted conversation '{conversation_id}'",
            "deleted_chunks": deleted_chunks,
            "conversation_id": conversation_id,
            "client_id": client_id,
        },
    )


async def delete_conversation(
    conversation_id: str, user: User, permanent: bool = False
):
    """
    Soft delete a conversation (mark as deleted but keep data).

    Args:
        conversation_id: Conversation to delete
        user: Requesting user
        permanent: If True, permanently delete (admin only)
    """
    try:
        # Create masked identifier for logging
        masked_id = (
            f"{conversation_id[:8]}...{conversation_id[-4:]}"
            if len(conversation_id) > 12
            else "***"
        )
        logger.info(
            f"Attempting to {'permanently ' if permanent else ''}delete conversation: {masked_id}"
        )

        conversation, error = await _get_conversation_or_error(conversation_id, user)
        if error:
            if error.status_code == 403:
                logger.warning(
                    f"User {user.user_id} attempted to delete conversation {conversation_id} without permission"
                )
            return error

        # Hard delete (admin only, permanent flag)
        if permanent and user.is_superuser:
            return await _hard_delete_conversation(conversation)

        # Soft delete (default)
        return await _soft_delete_conversation(conversation, user)

    except Exception as e:
        logger.error(f"Error deleting conversation {conversation_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to delete conversation: {str(e)}"},
        )


async def archive_conversation_audio_doc(
    conversation: Conversation, reason: str = "manual_cleanup"
) -> int:
    """Archive a conversation document's audio (no permission check).

    Hard-deletes the audio chunks and marks the conversation archived + soft-
    deleted, keeping duration as the stub metadata. Returns the number of audio
    chunks deleted. Idempotent: a re-run on an already-archived conversation
    backfills the soft-delete flags and deletes 0 chunks.

    This is the shared core used by both the user-facing endpoint (after a
    permission check) and the system auto-clean sweep. Archiving is a
    specialization of soft-delete: ``deleted=True`` hides it from the normal
    list and surfaces it in the Archive tab; ``audio_archived``/``archive_reason``
    record that the audio bytes were permanently purged. ``deletion_reason
    ="audio_archived"`` is intentionally distinct from the no-speech/orphan
    reasons so it isn't treated as a reprocessable orphan.
    """
    conversation_id = conversation.conversation_id
    archived_at = datetime.now(timezone.utc)

    if conversation.audio_archived:
        # Idempotent backfill of soft-delete flags for items archived before
        # archiving was unified with soft-delete.
        if not conversation.deleted:
            conversation.deleted = True
            conversation.deletion_reason = "audio_archived"
            conversation.deleted_at = conversation.audio_archived_at or archived_at
            await conversation.save()
        return 0

    # 1. Hard delete audio chunks FIRST (no rollback for hard deletes; if the
    #    metadata write fails afterwards a re-run completes — chunks are gone).
    result = await AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conversation_id
    ).delete()
    deleted_chunks = result.deleted_count

    # 2. Mark the conversation as archived (+ soft-deleted), keeping duration.
    conversation.audio_archived = True
    conversation.audio_archived_at = archived_at
    conversation.archive_reason = reason
    conversation.audio_chunks_count = 0
    conversation.audio_compression_ratio = None
    conversation.vad_analysis = None  # derived from chunks that no longer exist
    conversation.deleted = True
    conversation.deletion_reason = "audio_archived"
    conversation.deleted_at = archived_at
    await conversation.save()

    # Drop any cached waveform — it points at audio that no longer exists.
    try:
        await WaveformData.find(
            WaveformData.conversation_id == conversation_id
        ).delete()
    except Exception as e:
        logger.warning(f"Failed to delete waveform for {conversation_id}: {e}")

    logger.info(
        f"Archived audio for conversation {conversation_id} "
        f"(reason={reason}, deleted {deleted_chunks} chunks)"
    )
    return deleted_chunks


async def archive_conversation_audio(
    conversation_id: str, user: User, reason: str = "manual_cleanup"
) -> JSONResponse:
    """Archive a conversation's audio: permanently delete the audio bytes from
    MongoDB while keeping the conversation document as a lightweight metadata
    stub (date, duration, reason).

    Used by the Data Audit feature to reclaim storage for speech-free or
    bad-speaker recordings. Unlike soft delete, this is irreversible for the
    audio — the transcript/segment metadata is retained.
    """
    conversation, error = await _get_conversation_or_error(conversation_id, user)
    if error:
        return error

    already_archived = conversation.audio_archived
    deleted_chunks = await archive_conversation_audio_doc(conversation, reason)

    if already_archived:
        return JSONResponse(
            status_code=200,
            content={
                "message": "Conversation audio already archived",
                "conversation_id": conversation_id,
                "already_archived": True,
                "deleted_chunks": 0,
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "message": f"Successfully archived audio for conversation '{conversation_id}'",
            "conversation_id": conversation_id,
            "archive_reason": reason,
            "deleted_chunks": deleted_chunks,
            "duration_seconds": conversation.audio_total_duration,
        },
    )


async def restore_conversation(conversation_id: str, user: User) -> JSONResponse:
    """
    Restore a soft-deleted conversation.

    Args:
        conversation_id: Conversation to restore
        user: Requesting user
    """
    try:
        conversation, error = await _get_conversation_or_error(conversation_id, user)
        if error:
            return error

        if not conversation.deleted:
            return JSONResponse(
                status_code=400, content={"error": "Conversation is not deleted"}
            )

        # 1. Restore audio chunks FIRST (safe failure mode: restored chunks, conversation still deleted)
        original_deleted_at = conversation.deleted_at
        result = await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id,
            AudioChunkDocument.deleted == True,
        ).update_many({"$set": {"deleted": False, "deleted_at": None}})

        restored_chunks = result.modified_count

        # 2. Restore conversation
        conversation.deleted = False
        conversation.deletion_reason = None
        conversation.deleted_at = None
        try:
            await conversation.save()
        except Exception:
            # Rollback: re-soft-delete the chunks we just restored
            logger.error(
                f"Failed to restore conversation {conversation_id}, "
                f"rolling back {restored_chunks} chunk restores"
            )
            await AudioChunkDocument.find(
                AudioChunkDocument.conversation_id == conversation_id,
                AudioChunkDocument.deleted == False,
            ).update_many(
                {"$set": {"deleted": True, "deleted_at": original_deleted_at}}
            )
            raise

        logger.info(
            f"Restored conversation {conversation_id} "
            f"({restored_chunks} chunks) for user {user.user_id}"
        )

        return JSONResponse(
            status_code=200,
            content={
                "message": f"Successfully restored conversation '{conversation_id}'",
                "restored_chunks": restored_chunks,
                "conversation_id": conversation_id,
            },
        )

    except Exception as e:
        logger.error(f"Error restoring conversation {conversation_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to restore conversation: {str(e)}"},
        )


async def _restore_if_deleted_and_prepare(
    conversation: Conversation,
    conversation_id: str,
    processing_status: str | None = Conversation.ConversationStatus.ACTIVE.value,
) -> None:
    """Restore soft-deleted conversation/chunks and optionally set processing_status."""
    changed = False

    if conversation.deleted:
        await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id,
            AudioChunkDocument.deleted == True,
        ).update_many({"$set": {"deleted": False, "deleted_at": None}})
        conversation.deleted = False
        conversation.deletion_reason = None
        conversation.deleted_at = None
        changed = True

    if (
        processing_status is not None
        and conversation.processing_status != processing_status
    ):
        conversation.processing_status = processing_status
        changed = True

    if changed:
        await conversation.save()


def _enqueue_transcript_reprocessing(
    conversation_id: str,
    user_id: str,
    source: str,
    job_id_prefix: str,
    end_reason: str,
) -> tuple:
    """Enqueue transcribe job + post-conversation chain.

    Returns (version_id, transcript_job, post_jobs dict).
    """
    version_id = str(uuid.uuid4())

    transcript_job = transcription_queue.enqueue(
        transcribe_full_audio_job,
        conversation_id,
        version_id,
        source,
        job_timeout=-1,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"{job_id_prefix}_{conversation_id[:8]}",
        description=f"Transcribe audio for {conversation_id[:8]}",
        meta={"conversation_id": conversation_id},
    )

    post_jobs = start_post_conversation_jobs(
        conversation_id=conversation_id,
        user_id=user_id,
        transcript_version_id=version_id,
        depends_on_job=transcript_job,
        end_reason=end_reason,
        memory_cause=MemoryCause.TRANSCRIPT_REPROCESS,
    )

    return version_id, transcript_job, post_jobs


def _resolve_transcript_version(conversation: Conversation, version_id: str) -> tuple:
    """Resolve 'active' to real version ID and find the version object.

    Returns (error_response_or_None, resolved_version_id, version_object).
    If error_response is not None, the caller should return it immediately.
    """
    resolved_id = version_id
    if resolved_id == "active":
        active_id = conversation.active_transcript_version
        if not active_id:
            return (
                JSONResponse(
                    status_code=404,
                    content={"error": "No active transcript version found"},
                ),
                None,
                None,
            )
        resolved_id = active_id

    version_obj = conversation.get_transcript_version(resolved_id)
    if not version_obj:
        return (
            JSONResponse(
                status_code=404,
                content={"error": f"Transcript version '{resolved_id}' not found"},
            ),
            None,
            None,
        )

    return None, resolved_id, version_obj


def _enqueue_speaker_reprocessing_chain(
    conversation_id: str,
    version_id: str,
    source_version_id: str,
    diarization_source: str | None = None,
) -> dict:
    """Enqueue speaker -> memory -> title_summary chain.

    Returns dict with keys: speaker, memory, title_summary (job IDs).
    """
    speaker_job = transcription_queue.enqueue(
        recognise_speakers_job,
        conversation_id,
        version_id,
        "",  # transcript_text: read from source version
        None,  # words: read from source version
        source_version_id,  # create-on-success: read from this version, create version_id
        diarization_source,
        job_timeout=1200,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"reprocess_speaker_{conversation_id[:12]}",
        description=f"Re-diarize speakers for {conversation_id[:8]}",
        **post_conv_enqueue_kwargs(
            "speaker",
            {
                "conversation_id": conversation_id,
                "version_id": version_id,
                "source_version_id": source_version_id,
                "diarization_source": diarization_source,
                "trigger": "reprocess",
            },
        ),
    )
    logger.info(
        f"Enqueued speaker reprocessing job {speaker_job.id} for version {version_id}"
    )

    memory_job = memory_queue.enqueue(
        process_memory_job,
        conversation_id,
        job_timeout=1800,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"memory_{conversation_id[:12]}",
        description=f"Extract memories for {conversation_id[:8]}",
        **post_conv_enqueue_kwargs(
            "memory",
            {
                "conversation_id": conversation_id,
                "cause": MemoryCause.SPEAKER_REPROCESS.value,
                "strategy": UpdateStrategy.SPEAKER_DIFF.value,
            },
            depends_on=speaker_job,
        ),
    )
    logger.info(
        f"Chained memory job {memory_job.id} after speaker job {speaker_job.id}"
    )

    title_summary_job = default_queue.enqueue(
        generate_title_summary_job,
        conversation_id,
        job_timeout=300,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"title_summary_{conversation_id[:12]}",
        description=f"Regenerate title/summary for {conversation_id[:8]}",
        **post_conv_enqueue_kwargs(
            "title_summary",
            {"conversation_id": conversation_id},
            depends_on=memory_job,
        ),
    )
    logger.info(
        f"Chained title/summary job {title_summary_job.id} after memory job {memory_job.id}"
    )

    return {
        "speaker": speaker_job.id,
        "memory": memory_job.id,
        "title_summary": title_summary_job.id,
    }


async def toggle_star(conversation_id: str, user: User):
    """Toggle the starred/favorite status of a conversation."""
    try:
        conversation, error = await _get_conversation_or_error(conversation_id, user)
        if error:
            return error

        # Toggle
        conversation.starred = not conversation.starred
        conversation.starred_at = (
            datetime.now(timezone.utc) if conversation.starred else None
        )
        await conversation.save()

        logger.info(
            f"Conversation {conversation_id} {'starred' if conversation.starred else 'unstarred'} "
            f"by user {user.user_id}"
        )

        # Dispatch plugin event (fire-and-forget)
        try:
            plugin_router = get_plugin_router()
            if plugin_router:
                await plugin_router.dispatch_event(
                    event=PluginEvent.CONVERSATION_STARRED,
                    user_id=str(user.user_id),
                    data={
                        "conversation_id": conversation_id,
                        "starred": conversation.starred,
                        "starred_at": (
                            conversation.starred_at.isoformat()
                            if conversation.starred_at
                            else None
                        ),
                        "title": conversation.title,
                    },
                )
        except Exception as e:
            logger.warning(f"Failed to dispatch conversation.starred event: {e}")

        return {
            "conversation_id": conversation_id,
            "starred": conversation.starred,
            "starred_at": (
                conversation.starred_at.isoformat() if conversation.starred_at else None
            ),
        }

    except Exception as e:
        logger.error(f"Error toggling star for conversation {conversation_id}: {e}")
        return JSONResponse(status_code=500, content={"error": "Error toggling star"})


async def reprocess_orphan(conversation_id: str, user: User):
    """Reprocess an orphan audio session - restore if deleted and enqueue full processing chain."""
    try:
        conversation, error = await _get_conversation_or_error(conversation_id, user)
        if error:
            return error

        # Verify audio chunks exist (check both deleted and non-deleted)
        total_chunks = await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id
        ).count()

        if total_chunks == 0:
            return JSONResponse(
                status_code=400,
                content={"error": "No audio data found for this conversation"},
            )

        # If conversation is soft-deleted, restore it and its chunks
        if conversation.deleted:
            await AudioChunkDocument.find(
                AudioChunkDocument.conversation_id == conversation_id,
                AudioChunkDocument.deleted == True,
            ).update_many({"$set": {"deleted": False, "deleted_at": None}})

            conversation.deleted = False
            conversation.deletion_reason = None
            conversation.deleted_at = None

        # Back to in-flight; the finalizer reconciles the terminal status when the
        # reprocess chain completes.
        conversation.processing_status = Conversation.ConversationStatus.ACTIVE.value
        conversation.failure_stage = None
        conversation.title = "Reprocessing..."
        conversation.summary = None
        conversation.detailed_summary = None
        await conversation.save()

        # Enqueue the same job chain as reprocess_transcript
        version_id, transcript_job, post_jobs = _enqueue_transcript_reprocessing(
            conversation_id=conversation_id,
            user_id=str(user.user_id),
            source="reprocess_orphan",
            job_id_prefix="orphan_transcribe",
            end_reason="reprocess_orphan",
        )

        logger.info(
            f"Enqueued orphan reprocessing chain for {conversation_id}: "
            f"transcribe={transcript_job.id} → post_jobs={post_jobs}"
        )

        return JSONResponse(
            content={
                "message": f"Orphan reprocessing started for conversation {conversation_id}",
                "job_id": transcript_job.id,
                "title_summary_job_id": post_jobs.get("title_summary"),
                "version_id": version_id,
                "status": "queued",
            }
        )

    except Exception as e:
        logger.error(f"Error starting orphan reprocessing for {conversation_id}: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error starting orphan reprocessing"}
        )


async def reprocess_transcript(conversation_id: str, user: User):
    """Reprocess transcript for a conversation. Users can only reprocess their own conversations."""
    try:
        conversation_model, error = await _get_conversation_or_error(
            conversation_id, user
        )
        if error:
            return error

        await _restore_if_deleted_and_prepare(conversation_model, conversation_id)

        # Get audio_uuid from conversation
        # Validate audio chunks exist in MongoDB
        chunks = await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id
        ).to_list()

        if not chunks:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "No audio data found for this conversation",
                    "details": f"Conversation '{conversation_id}' exists but has no audio chunks in MongoDB",
                },
            )

        # Enqueue transcription + post-conversation job chain
        version_id, transcript_job, post_jobs = _enqueue_transcript_reprocessing(
            conversation_id=conversation_id,
            user_id=str(user.user_id),
            source="reprocess",
            job_id_prefix="reprocess",
            end_reason="reprocess_transcript",
        )

        logger.info(
            f"Created transcript reprocessing job {transcript_job.id} (version: {version_id}) "
            f"for conversation {conversation_id}, post_jobs={post_jobs}"
        )

        return JSONResponse(
            content={
                "message": f"Transcript reprocessing started for conversation {conversation_id}",
                "job_id": transcript_job.id,
                "title_summary_job_id": post_jobs.get("title_summary"),
                "version_id": version_id,
                "status": "queued",
            }
        )

    except Exception as e:
        logger.error(f"Error starting transcript reprocessing: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error starting transcript reprocessing"}
        )


async def reprocess_memory(
    conversation_id: str, transcript_version_id: str, user: User
):
    """Reprocess memory extraction for a specific transcript version. Users can only reprocess their own conversations."""
    try:
        conversation_model, error = await _get_conversation_or_error(
            conversation_id, user
        )
        if error:
            return error

        await _restore_if_deleted_and_prepare(
            conversation_model, conversation_id, processing_status=None
        )

        # Resolve transcript version ID (handle "active" special case)
        error, transcript_version_id, transcript_version = _resolve_transcript_version(
            conversation_model, transcript_version_id
        )
        if error:
            return error

        # Create new memory version ID
        version_id = str(uuid.uuid4())

        # Enqueue memory processing job with RQ (RQ handles job tracking)

        job = enqueue_memory_processing(
            conversation_id=conversation_id,
            priority=JobPriority.NORMAL,
            cause=MemoryCause.MEMORY_REPLAY,
            strategy=UpdateStrategy.FULL,
        )

        logger.info(
            f"Created memory reprocessing job {job.id} (version {version_id}) for conversation {conversation_id}"
        )

        return JSONResponse(
            content={
                "message": f"Memory reprocessing started for conversation {conversation_id}",
                "job_id": job.id,
                "version_id": version_id,
                "transcript_version_id": transcript_version_id,
                "status": "queued",
            }
        )

    except Exception as e:
        logger.error(f"Error starting memory reprocessing: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error starting memory reprocessing"}
        )


async def reprocess_speakers(
    conversation_id: str,
    transcript_version_id: str,
    user: User,
    diarization_source: str | None = None,
):
    """
    Reprocess speaker identification for a specific transcript version.
    Users can only reprocess their own conversations.

    Creates NEW transcript version with same text/words but re-identified speakers.
    Automatically chains memory reprocessing since speaker attribution affects meaning.
    """
    try:
        # 1. Find conversation and validate ownership
        conversation_model, error = await _get_conversation_or_error(
            conversation_id, user
        )
        if error:
            return error

        # Re-diarization operates on an existing transcript (text is copied to the
        # new version), so the conversation stays "completed" throughout. Don't flip
        # it to "active": the speaker-reprocess chain (speaker->memory->title_summary)
        # has no finalizer, so an "active" flip would never settle back. Restore from
        # soft-delete but keep the fact-derived status, then settle it explicitly.
        await _restore_if_deleted_and_prepare(
            conversation_model, conversation_id, processing_status=None
        )
        if conversation_model.apply_status(settled=True):
            await conversation_model.save()

        # Single-flight: reject if a reprocess chain is already running for this
        # conversation. Overlapping chains (e.g. rapid repeat clicks) race on the
        # conversation's full-document save() and a stale chain can clobber the newer
        # speaker write — leaving an orphan version with empty speaker metadata and
        # unchanged labels. Bail before creating a new version so we don't pile up
        # dead versions either.
        in_flight = conversation_edit_chain_in_flight(conversation_id)
        if in_flight:
            logger.info(
                f"Reprocess already in flight for {conversation_id[:8]} "
                f"(job {in_flight}); skipping duplicate request"
            )
            return JSONResponse(
                status_code=409,
                content={
                    "error": "Speaker reprocessing is already in progress for this conversation.",
                    "in_flight_job_id": in_flight,
                },
            )

        # 2-3. Resolve source transcript version ID and find version object
        error, source_version_id, source_version = _resolve_transcript_version(
            conversation_model, transcript_version_id
        )
        if error:
            return error

        # A Pyannote reprocess replaces the version's segments. When the user asks
        # to switch back to provider diarization, follow the reprocess lineage to
        # the nearest version that still contains the original provider segments.
        if (
            diarization_source == "provider"
            and source_version.diarization_source != "provider"
        ):
            visited = {source_version.version_id}
            lineage_version = source_version
            while lineage_version.metadata:
                parent_id = lineage_version.metadata.get("source_version_id")
                if not parent_id or parent_id in visited:
                    break
                visited.add(parent_id)
                parent = conversation_model.get_transcript_version(parent_id)
                if not parent:
                    break
                if parent.diarization_source == "provider":
                    source_version = parent
                    source_version_id = parent.version_id
                    break
                lineage_version = parent

            if source_version.diarization_source != "provider":
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": (
                            "No provider-diarized source version is available "
                            "for this transcript."
                        )
                    },
                )

        # 4. Validate transcript has content and words (or provider-diarized segments)
        if not source_version.transcript:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Cannot re-diarize empty transcript. Transcript version has no text."
                },
            )

        provider_capabilities = source_version.metadata.get("provider_capabilities", {})
        provider_has_diarization = (
            provider_capabilities.get("diarization", False)
            or source_version.diarization_source == "provider"
        )
        has_words = bool(source_version.words)
        has_segments = bool(source_version.segments)

        if not has_words and not has_segments:
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        "Cannot re-diarize transcript without word timings or segments. "
                        "Word timestamps or provider segments are required."
                    )
                },
            )
        if not has_words and has_segments and not provider_has_diarization:
            logger.warning(
                "Reprocessing speakers without word timings; "
                "falling back to segment-based identification only."
            )

        # 5. Check if speaker recognition is enabled
        speaker_config = get_service_config("speaker_recognition")
        if not speaker_config.get("enabled", True):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Speaker recognition is disabled",
                    "details": "Enable speaker service in config to use this feature",
                },
            )

        # 6. Pre-allocate the new version id but DON'T create it yet. The speaker job
        #    reads from the source version and creates this version only once it has a
        #    usable result — mirroring transcript reprocess (transcribe_full_audio_job).
        #    A failed/empty reprocess therefore leaves NO new version behind and surfaces
        #    an error, instead of a degraded no-op version with unchanged labels.
        new_version_id = str(uuid.uuid4())

        # 7-8. Enqueue speaker → memory → title/summary chain (create-on-success).
        job_ids = _enqueue_speaker_reprocessing_chain(
            conversation_id,
            new_version_id,
            source_version_id,
            diarization_source,
        )

        # 9. Return job information
        return JSONResponse(
            content={
                "message": "Speaker reprocessing started",
                "job_id": job_ids["speaker"],
                "memory_job_id": job_ids["memory"],
                "title_summary_job_id": job_ids["title_summary"],
                "version_id": new_version_id,
                "source_version_id": source_version_id,
                "diarization_source": diarization_source,
                "status": "queued",
            }
        )

    except Exception as e:
        logger.error(f"Error starting speaker reprocessing: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error starting speaker reprocessing"}
        )


async def activate_transcript_version(
    conversation_id: str, version_id: str, user: User
):
    """Activate a specific transcript version. Users can only modify their own conversations."""
    try:
        conversation_model, error = await _get_conversation_or_error(
            conversation_id, user
        )
        if error:
            return error

        # Activate the transcript version using Beanie model method
        success = conversation_model.set_active_transcript_version(version_id)
        if not success:
            return JSONResponse(
                status_code=400,
                content={"error": "Failed to activate transcript version"},
            )

        await conversation_model.save()

        # TODO: Trigger speaker recognition if configured
        # This would integrate with existing speaker recognition logic

        logger.info(
            f"Activated transcript version {version_id} for conversation {conversation_id} by user {user.user_id}"
        )

        return JSONResponse(
            content={
                "message": f"Transcript version {version_id} activated successfully",
                "active_transcript_version": version_id,
            }
        )

    except Exception as e:
        logger.error(f"Error activating transcript version: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error activating transcript version"}
        )


async def get_conversation_version_history(conversation_id: str, user: User):
    """Get transcript version history for a conversation. Users can only access their own conversations.

    Memory is no longer versioned (the vault is the system of record); see the
    memory audit ledger (``get_conversation_memory_audit``) for memory change history.
    """
    try:
        conversation_model, error = await _get_conversation_or_error(
            conversation_id, user
        )
        if error:
            return error

        # Get version history from model
        # Convert datetime objects to ISO strings for JSON serialization
        transcript_versions = []
        for v in conversation_model.transcript_versions:
            version_dict = v.model_dump()
            if version_dict.get("created_at"):
                version_dict["created_at"] = version_dict["created_at"].isoformat()
            transcript_versions.append(version_dict)

        history = {
            "conversation_id": conversation_id,
            "active_transcript_version": conversation_model.active_transcript_version,
            "transcript_versions": transcript_versions,
        }

        return JSONResponse(content=history)

    except Exception as e:
        logger.error(f"Error fetching version history: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error fetching version history"}
        )


async def get_conversation_memory_audit(
    conversation_id: str, user: User, limit: int = 100
):
    """Get the memory vault change history (audit ledger) for a conversation.

    Replaces the old per-conversation "memory versions": memory is a vault that is
    overwritten in place, so instead we return the recorded changes (which notes
    were created/updated/deleted, when, and what triggered each).
    """
    try:
        _, error = await _get_conversation_or_error(conversation_id, user)
        if error:
            return error

        entries = (
            await MemoryAuditEntry.find(
                MemoryAuditEntry.conversation_id == conversation_id
            )
            .sort(-MemoryAuditEntry.created_at)
            .limit(limit)
            .to_list()
        )

        return JSONResponse(
            content={
                "conversation_id": conversation_id,
                "count": len(entries),
                "entries": [_memory_audit_to_dict(e) for e in entries],
            }
        )

    except Exception as e:
        logger.error(f"Error fetching memory audit for {conversation_id}: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error fetching memory audit"}
        )


def _memory_audit_to_dict(entry) -> dict:
    """Serialize a MemoryAuditEntry for API responses.

    Provenance is exposed both raw (``cause``/``strategy``) and pre-classified
    (``source_kind``/``source_label``/``actor``) so the WebUI renders an honest
    label without re-deriving the taxonomy from magic strings.
    """
    return {
        "id": str(entry.id),
        "user_id": entry.user_id,
        "conversation_id": entry.conversation_id,
        "operation": entry.operation,
        "note_path": entry.note_path,
        "cause": entry.cause,
        "strategy": entry.strategy,
        "source_kind": source_kind_for(entry.cause, entry.agent_mode, entry.operation),
        "source_label": source_label_for(
            entry.cause, entry.agent_mode, entry.operation
        ),
        "actor": actor_for(entry.cause, entry.agent_mode, entry.operation),
        "provider": entry.provider,
        "agent_mode": entry.agent_mode,
        "before_hash": entry.before_hash,
        "after_hash": entry.after_hash,
        "after_bytes": entry.after_bytes,
        "summary": entry.summary,
        "extra": entry.extra,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        # Whether a before→after diff can be fetched for this entry. True when the
        # post-change content was retained, or for deletes (the prior recorded
        # change supplies the removed content). False for legacy entries written
        # before content was retained and for note-less delete_all operations.
        "has_diff": entry.after_text is not None or entry.operation == "delete",
    }
