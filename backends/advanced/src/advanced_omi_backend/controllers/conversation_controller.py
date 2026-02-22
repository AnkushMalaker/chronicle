"""
Conversation controller for handling conversation-related business logic.
"""

import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

import redis.asyncio as aioredis
from fastapi.responses import JSONResponse
from pymongo.errors import OperationFailure

from advanced_omi_backend.client_manager import (
    client_belongs_to_user,
    get_client_manager,
)
from advanced_omi_backend.config import get_transcription_job_timeout
from advanced_omi_backend.config_loader import get_service_config
from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    default_queue,
    memory_queue,
    start_post_conversation_jobs,
    transcription_queue,
)
from advanced_omi_backend.controllers.session_controller import (
    request_conversation_close,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import JobPriority
from advanced_omi_backend.plugins.events import ConversationCloseReason, PluginEvent
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.users import User
from advanced_omi_backend.workers.conversation_jobs import generate_title_summary_job
from advanced_omi_backend.workers.memory_jobs import (
    enqueue_memory_processing,
    process_memory_job,
)
from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job

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

    session_id = getattr(client_state, "stream_session_id", None)
    if not session_id:
        return JSONResponse(
            content={"error": "No active session"},
            status_code=400,
        )

    # Signal the conversation job to close and trigger post-processing
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url)
    try:
        success = await request_conversation_close(
            r, session_id, reason=ConversationCloseReason.USER_REQUESTED.value
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
            "memory_count": conversation.memory_count,
            "has_memory": conversation.has_memory,
            "active_transcript_version": conversation.active_transcript_version,
            "active_memory_version": conversation.active_memory_version,
            "transcript_version_count": conversation.transcript_version_count,
            "memory_version_count": conversation.memory_version_count,
            "active_transcript_version_number": conversation.active_transcript_version_number,
            "active_memory_version_number": conversation.active_memory_version_number,
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
        "processing_status": conv.processing_status,
        "always_persist": conv.always_persist,
        "title": conv.title,
        "summary": conv.summary,
        "detailed_summary": conv.detailed_summary,
        "active_transcript_version": conv.active_transcript_version,
        "active_memory_version": conv.active_memory_version,
        "segment_count": conv.segment_count,
        "has_memory": conv.has_memory,
        "memory_count": conv.memory_count,
        "transcript_version_count": conv.transcript_version_count,
        "memory_version_count": conv.memory_version_count,
        "active_transcript_version_number": conv.active_transcript_version_number,
        "active_memory_version_number": conv.active_memory_version_number,
        "starred": conv.starred,
        "starred_at": conv.starred_at.isoformat() if conv.starred_at else None,
    }


def _raw_doc_to_list_dict(doc: dict) -> dict:
    """Convert a raw pymongo document (projected) to a list-view dict.

    Computes segment_count, memory_count etc. from the lightweight projected
    version arrays without loading full transcript/word data.
    """
    active_tv = doc.get("active_transcript_version")
    active_mv = doc.get("active_memory_version")

    # Compute segment_count from projected transcript_versions
    segment_count = 0
    transcript_versions = doc.get("transcript_versions") or []
    for tv in transcript_versions:
        if tv.get("version_id") == active_tv:
            segment_count = len(tv.get("segments", []))
            break

    # Compute memory_count from projected memory_versions
    memory_count = 0
    memory_versions = doc.get("memory_versions") or []
    for mv in memory_versions:
        if mv.get("version_id") == active_mv:
            memory_count = mv.get("memory_count", 0)
            break

    # Compute active version numbers (1-based)
    active_transcript_version_number = None
    for i, tv in enumerate(transcript_versions):
        if tv.get("version_id") == active_tv:
            active_transcript_version_number = i + 1
            break

    active_memory_version_number = None
    for i, mv in enumerate(memory_versions):
        if mv.get("version_id") == active_mv:
            active_memory_version_number = i + 1
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
        "processing_status": doc.get("processing_status"),
        "always_persist": doc.get("always_persist", False),
        "title": doc.get("title"),
        "summary": doc.get("summary"),
        "detailed_summary": doc.get("detailed_summary"),
        "active_transcript_version": active_tv,
        "active_memory_version": active_mv,
        "segment_count": segment_count,
        "has_memory": len(memory_versions) > 0,
        "memory_count": memory_count,
        "transcript_version_count": len(transcript_versions),
        "memory_version_count": len(memory_versions),
        "active_transcript_version_number": active_transcript_version_number,
        "active_memory_version_number": active_memory_version_number,
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
    "processing_status": 1,
    "always_persist": 1,
    "title": 1,
    "summary": 1,
    "detailed_summary": 1,
    "starred": 1,
    "starred_at": 1,
    "active_transcript_version": 1,
    "active_memory_version": 1,
    # Lightweight version metadata (exclude transcript, words, segment text)
    "transcript_versions.version_id": 1,
    "transcript_versions.segments": 1,
    "memory_versions.version_id": 1,
    "memory_versions.memory_count": 1,
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
            # Orphan type 1: always_persist stuck in pending/failed (not deleted)
            conditions.append(
                {
                    "always_persist": True,
                    "processing_status": {
                        "$in": ["pending_transcription", "transcription_failed"]
                    },
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
                    in ("pending_transcription", "transcription_failed")
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


async def search_conversations(
    query: str,
    user: User,
    limit: int = 50,
    offset: int = 0,
):
    """Full-text search across conversation titles, summaries, and transcripts."""
    try:
        collection = Conversation.get_pymongo_collection()

        match_filter: dict = {"$text": {"$search": query}, "deleted": False}
        if not user.is_superuser:
            match_filter["user_id"] = str(user.user_id)

        pipeline = [
            {"$match": match_filter},
            {"$addFields": {"score": {"$meta": "textScore"}}},
            {"$sort": {"score": -1}},
            {
                "$facet": {
                    "results": [
                        {"$skip": offset},
                        {"$limit": limit},
                        {"$project": {**_LIST_PROJECTION, "score": 1}},
                    ],
                    "count": [{"$count": "total"}],
                }
            },
        ]

        try:
            cursor = collection.aggregate(pipeline)
            facet_result = await cursor.to_list(length=1)
        except OperationFailure as op_err:
            if op_err.code == 27:  # No text index
                logger.warning(
                    "Text search failed: no text index on conversations collection. "
                    "Restart the backend to let Beanie create the index."
                )
                return {
                    "conversations": [],
                    "total": 0,
                    "limit": limit,
                    "offset": offset,
                    "query": query,
                    "error": "Text search index not available. Try restarting the backend.",
                }
            raise

        facet = facet_result[0] if facet_result else {"results": [], "count": []}

        raw_docs = facet.get("results", [])
        count_list = facet.get("count", [])
        total = count_list[0]["total"] if count_list else 0

        conversations = []
        for doc in raw_docs:
            score = doc.pop("score", 0)
            d = _raw_doc_to_list_dict(doc)
            d["score"] = round(score, 4)
            d["is_orphan"] = False
            conversations.append(d)

        return {
            "conversations": conversations,
            "total": total,
            "limit": limit,
            "offset": offset,
            "query": query,
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
    deleted_at = datetime.utcnow()

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
    from advanced_omi_backend.workers.transcription_jobs import (
        transcribe_full_audio_job,
    )

    version_id = str(uuid.uuid4())

    transcript_job = transcription_queue.enqueue(
        transcribe_full_audio_job,
        conversation_id,
        version_id,
        source,
        job_timeout=get_transcription_job_timeout(),
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

    version_obj = None
    for v in conversation.transcript_versions:
        if v.version_id == resolved_id:
            version_obj = v
            break

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
) -> dict:
    """Enqueue speaker -> memory -> title_summary chain.

    Returns dict with keys: speaker, memory, title_summary (job IDs).
    """
    speaker_job = transcription_queue.enqueue(
        recognise_speakers_job,
        conversation_id,
        version_id,
        job_timeout=1200,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"reprocess_speaker_{conversation_id[:12]}",
        description=f"Re-diarize speakers for {conversation_id[:8]}",
        meta={
            "conversation_id": conversation_id,
            "version_id": version_id,
            "source_version_id": source_version_id,
            "trigger": "reprocess",
        },
    )
    logger.info(
        f"Enqueued speaker reprocessing job {speaker_job.id} for version {version_id}"
    )

    memory_job = memory_queue.enqueue(
        process_memory_job,
        conversation_id,
        depends_on=speaker_job,
        job_timeout=1800,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"memory_{conversation_id[:12]}",
        description=f"Extract memories for {conversation_id[:8]}",
        meta={"conversation_id": conversation_id, "trigger": "reprocess_after_speaker"},
    )
    logger.info(
        f"Chained memory job {memory_job.id} after speaker job {speaker_job.id}"
    )

    title_summary_job = default_queue.enqueue(
        generate_title_summary_job,
        conversation_id,
        job_timeout=300,
        result_ttl=JOB_RESULT_TTL,
        depends_on=memory_job,
        job_id=f"title_summary_{conversation_id[:12]}",
        description=f"Regenerate title/summary for {conversation_id[:8]}",
        meta={"conversation_id": conversation_id, "trigger": "reprocess_after_speaker"},
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
        conversation.starred_at = datetime.utcnow() if conversation.starred else None
        await conversation.save()

        logger.info(
            f"Conversation {conversation_id} {'starred' if conversation.starred else 'unstarred'} "
            f"by user {user.user_id}"
        )

        # Dispatch plugin event (fire-and-forget)
        try:
            from advanced_omi_backend.services.plugin_service import get_plugin_router

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

        # Set processing status and update title
        conversation.processing_status = "reprocessing"
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
    conversation_id: str, transcript_version_id: str, user: User
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
        # 2-3. Resolve source transcript version ID and find version object
        error, source_version_id, source_version = _resolve_transcript_version(
            conversation_model, transcript_version_id
        )
        if error:
            return error

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

        # 6. Create NEW transcript version (copy text/words, segments for provider-diarized)
        new_version_id = str(uuid.uuid4())

        # For provider-diarized transcripts, copy segments so the speaker job can
        # identify speakers per-segment. For word-based transcripts, leave segments
        # empty so pyannote can re-diarize.
        new_metadata = {
            "reprocessing_type": "speaker_diarization",
            "source_version_id": source_version_id,
            "trigger": "manual_reprocess",
            "provider_capabilities": provider_capabilities,
        }
        use_segments = provider_has_diarization or not has_words
        if use_segments:
            new_segments = source_version.segments  # COPY provider segments
            if not has_words and not provider_has_diarization:
                new_metadata["segments_only"] = True
        else:
            new_segments = []  # Empty - will be populated by speaker job

        new_version = conversation_model.add_transcript_version(
            version_id=new_version_id,
            transcript=source_version.transcript,  # COPY transcript text
            words=source_version.words,  # COPY word timings
            segments=new_segments,
            provider=source_version.provider,
            model=source_version.model,
            processing_time_seconds=None,  # Will be updated by job
            metadata=new_metadata,
            set_as_active=True,  # Set new version as active
        )

        # Carry over diarization_source so speaker job knows to use segment identification
        if provider_has_diarization or (not has_words and has_segments):
            new_version.diarization_source = "provider"

        # Save conversation with new version
        await conversation_model.save()

        logger.info(
            f"Created new transcript version {new_version_id} from source {source_version_id} "
            f"for conversation {conversation_id}"
        )

        # 7-8. Enqueue speaker → memory → title/summary chain
        job_ids = _enqueue_speaker_reprocessing_chain(
            conversation_id,
            new_version_id,
            source_version_id,
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


async def activate_memory_version(conversation_id: str, version_id: str, user: User):
    """Activate a specific memory version. Users can only modify their own conversations."""
    try:
        conversation_model, error = await _get_conversation_or_error(
            conversation_id, user
        )
        if error:
            return error

        # Activate the memory version using Beanie model method
        success = conversation_model.set_active_memory_version(version_id)
        if not success:
            return JSONResponse(
                status_code=400, content={"error": "Failed to activate memory version"}
            )

        await conversation_model.save()

        logger.info(
            f"Activated memory version {version_id} for conversation {conversation_id} by user {user.user_id}"
        )

        return JSONResponse(
            content={
                "message": f"Memory version {version_id} activated successfully",
                "active_memory_version": version_id,
            }
        )

    except Exception as e:
        logger.error(f"Error activating memory version: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error activating memory version"}
        )


async def get_conversation_version_history(conversation_id: str, user: User):
    """Get version history for a conversation. Users can only access their own conversations."""
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

        memory_versions = []
        for v in conversation_model.memory_versions:
            version_dict = v.model_dump()
            if version_dict.get("created_at"):
                version_dict["created_at"] = version_dict["created_at"].isoformat()
            memory_versions.append(version_dict)

        history = {
            "conversation_id": conversation_id,
            "active_transcript_version": conversation_model.active_transcript_version,
            "active_memory_version": conversation_model.active_memory_version,
            "transcript_versions": transcript_versions,
            "memory_versions": memory_versions,
        }

        return JSONResponse(content=history)

    except Exception as e:
        logger.error(f"Error fetching version history: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error fetching version history"}
        )
