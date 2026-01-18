"""
Conversation controller for handling conversation-related business logic.
"""

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi.responses import JSONResponse

from advanced_omi_backend.client_manager import (
    ClientManager,
    client_belongs_to_user,
)
from advanced_omi_backend.config_loader import get_service_config
from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    default_queue,
    memory_queue,
    transcription_queue,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import JobPriority
from advanced_omi_backend.users import User
from advanced_omi_backend.workers.memory_jobs import (
    enqueue_memory_processing,
    process_memory_job,
)
from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")

async def close_current_conversation(client_id: str, user: User, client_manager: ClientManager):
    """Close the current conversation for a specific client. Users can only close their own conversations."""
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

    if not client_manager.has_client(client_id):
        return JSONResponse(
            content={"error": f"Client '{client_id}' not found or not connected"},
            status_code=404,
        )

    client_state = client_manager.get_client(client_id)
    if client_state is None:
        return JSONResponse(
            content={"error": f"Client '{client_id}' not found or not connected"},
            status_code=404,
        )

    if not client_state.connected:
        return JSONResponse(
            content={"error": f"Client '{client_id}' is not connected"}, status_code=400
        )

    try:
        # Close the current conversation
        await client_state.close_current_conversation()

        # Reset conversation state but keep client connected
        client_state.current_audio_uuid = None
        client_state.conversation_start_time = time.time()
        client_state.last_transcript_time = None

        logger.info(f"Manually closed conversation for client {client_id} by user {user.id}")

        return JSONResponse(
            content={
                "message": f"Successfully closed current conversation for client '{client_id}'",
                "client_id": client_id,
                "timestamp": int(time.time()),
            }
        )

    except Exception as e:
        logger.error(f"Error closing conversation for client {client_id}: {e}")
        return JSONResponse(
            content={"error": f"Failed to close conversation: {str(e)}"},
            status_code=500,
        )


async def get_conversation(conversation_id: str, user: User):
    """Get a single conversation with full transcript details."""
    try:
        # Find the conversation using Beanie
        conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
        if not conversation:
            return JSONResponse(status_code=404, content={"error": "Conversation not found"})

        # Check ownership for non-admin users
        if not user.is_superuser and conversation.user_id != str(user.user_id):
            return JSONResponse(status_code=403, content={"error": "Access forbidden"})

        # Build response with explicit curated fields
        response = {
            "conversation_id": conversation.conversation_id,
            "user_id": conversation.user_id,
            "client_id": conversation.client_id,
            "audio_chunks_count": conversation.audio_chunks_count,
            "audio_total_duration": conversation.audio_total_duration,
            "audio_compression_ratio": conversation.audio_compression_ratio,
            "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
            "deleted": conversation.deleted,
            "deletion_reason": conversation.deletion_reason,
            "deleted_at": conversation.deleted_at.isoformat() if conversation.deleted_at else None,
            "end_reason": conversation.end_reason.value if conversation.end_reason else None,
            "completed_at": conversation.completed_at.isoformat() if conversation.completed_at else None,
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
        }

        return {"conversation": response}

    except Exception as e:
        logger.error(f"Error fetching conversation {conversation_id}: {e}")
        return JSONResponse(status_code=500, content={"error": "Error fetching conversation"})


async def get_conversations(user: User, include_deleted: bool = False):
    """Get conversations with speech only (speech-driven architecture)."""
    try:
        # Build query based on user permissions using Beanie
        if not user.is_superuser:
            # Regular users can only see their own conversations
            # Filter by deleted status
            if not include_deleted:
                user_conversations = await Conversation.find(
                    Conversation.user_id == str(user.user_id),
                    Conversation.deleted == False
                ).sort(-Conversation.created_at).to_list()
            else:
                user_conversations = await Conversation.find(
                    Conversation.user_id == str(user.user_id)
                ).sort(-Conversation.created_at).to_list()
        else:
            # Admins see all conversations
            # Filter by deleted status
            if not include_deleted:
                user_conversations = await Conversation.find(
                    Conversation.deleted == False
                ).sort(-Conversation.created_at).to_list()
            else:
                user_conversations = await Conversation.find_all().sort(-Conversation.created_at).to_list()

        # Build response with explicit curated fields - minimal for list view
        conversations = []
        for conv in user_conversations:
            conversations.append({
                "conversation_id": conv.conversation_id,
                "user_id": conv.user_id,
                "client_id": conv.client_id,
                "audio_chunks_count": conv.audio_chunks_count,
                "audio_total_duration": conv.audio_total_duration,
                "audio_compression_ratio": conv.audio_compression_ratio,
                "created_at": conv.created_at.isoformat() if conv.created_at else None,
                "deleted": conv.deleted,
                "deletion_reason": conv.deletion_reason,
                "deleted_at": conv.deleted_at.isoformat() if conv.deleted_at else None,
                "title": conv.title,
                "summary": conv.summary,
                "detailed_summary": conv.detailed_summary,
                "active_transcript_version": conv.active_transcript_version,
                "active_memory_version": conv.active_memory_version,
                # Computed fields (counts only, no heavy data)
                "segment_count": conv.segment_count,
                "has_memory": conv.has_memory,
                "memory_count": conv.memory_count,
                "transcript_version_count": conv.transcript_version_count,
                "memory_version_count": conv.memory_version_count,
            })

        return {"conversations": conversations}

    except Exception as e:
        logger.exception(f"Error fetching conversations: {e}")
        return JSONResponse(status_code=500, content={"error": "Error fetching conversations"})


async def _soft_delete_conversation(conversation: Conversation, user: User) -> JSONResponse:
    """Mark conversation and chunks as deleted (soft delete)."""
    conversation_id = conversation.conversation_id

    # Mark conversation as deleted
    conversation.deleted = True
    conversation.deletion_reason = "user_deleted"
    conversation.deleted_at = datetime.utcnow()
    await conversation.save()

    logger.info(f"Soft deleted conversation {conversation_id} for user {user.user_id}")

    # Soft delete all associated audio chunks
    result = await AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conversation_id,
        AudioChunkDocument.deleted == False  # Only update non-deleted chunks
    ).update_many({
        "$set": {
            "deleted": True,
            "deleted_at": datetime.utcnow()
        }
    })

    deleted_chunks = result.modified_count
    logger.info(f"Soft deleted {deleted_chunks} audio chunks for conversation {conversation_id}")

    return JSONResponse(
        status_code=200,
        content={
            "message": f"Successfully soft deleted conversation '{conversation_id}'",
            "deleted_chunks": deleted_chunks,
            "conversation_id": conversation_id,
            "client_id": conversation.client_id,
            "deleted_at": conversation.deleted_at.isoformat() if conversation.deleted_at else None
        }
    )


async def _hard_delete_conversation(conversation: Conversation) -> JSONResponse:
    """Permanently delete conversation and chunks (admin only)."""
    conversation_id = conversation.conversation_id
    client_id = conversation.client_id

    # Delete conversation document
    await conversation.delete()
    logger.info(f"Hard deleted conversation {conversation_id}")

    # Delete all audio chunks
    result = await AudioChunkDocument.find(
        AudioChunkDocument.conversation_id == conversation_id
    ).delete()

    deleted_chunks = result.deleted_count
    logger.info(f"Hard deleted {deleted_chunks} audio chunks for conversation {conversation_id}")

    return JSONResponse(
        status_code=200,
        content={
            "message": f"Successfully permanently deleted conversation '{conversation_id}'",
            "deleted_chunks": deleted_chunks,
            "conversation_id": conversation_id,
            "client_id": client_id
        }
    )


async def delete_conversation(conversation_id: str, user: User, permanent: bool = False):
    """
    Soft delete a conversation (mark as deleted but keep data).

    Args:
        conversation_id: Conversation to delete
        user: Requesting user
        permanent: If True, permanently delete (admin only)
    """
    try:
        # Create masked identifier for logging
        masked_id = f"{conversation_id[:8]}...{conversation_id[-4:]}" if len(conversation_id) > 12 else "***"
        logger.info(f"Attempting to {'permanently ' if permanent else ''}delete conversation: {masked_id}")

        # Find the conversation using Beanie
        conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)

        if not conversation:
            return JSONResponse(
                status_code=404,
                content={"error": f"Conversation '{conversation_id}' not found"}
            )

        # Check ownership for non-admin users
        if not user.is_superuser and conversation.user_id != str(user.user_id):
            logger.warning(
                f"User {user.user_id} attempted to delete conversation {conversation_id} without permission"
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Access forbidden. You can only delete your own conversations.",
                    "details": f"Conversation '{conversation_id}' does not belong to your account."
                }
            )

        # Hard delete (admin only, permanent flag)
        if permanent and user.is_superuser:
            return await _hard_delete_conversation(conversation)

        # Soft delete (default)
        return await _soft_delete_conversation(conversation, user)

    except Exception as e:
        logger.error(f"Error deleting conversation {conversation_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to delete conversation: {str(e)}"}
        )


async def restore_conversation(conversation_id: str, user: User) -> JSONResponse:
    """
    Restore a soft-deleted conversation.

    Args:
        conversation_id: Conversation to restore
        user: Requesting user
    """
    try:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )

        if not conversation:
            return JSONResponse(
                status_code=404,
                content={"error": "Conversation not found"}
            )

        # Permission check
        if not user.is_superuser and conversation.user_id != str(user.user_id):
            return JSONResponse(
                status_code=403,
                content={"error": "Access denied"}
            )

        if not conversation.deleted:
            return JSONResponse(
                status_code=400,
                content={"error": "Conversation is not deleted"}
            )

        # Restore conversation
        conversation.deleted = False
        conversation.deletion_reason = None
        conversation.deleted_at = None
        await conversation.save()

        # Restore audio chunks
        result = await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id,
            AudioChunkDocument.deleted == True
        ).update_many({
            "$set": {
                "deleted": False,
                "deleted_at": None
            }
        })

        restored_chunks = result.modified_count

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
            }
        )

    except Exception as e:
        logger.error(f"Error restoring conversation {conversation_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to restore conversation: {str(e)}"}
        )


async def reprocess_transcript(conversation_id: str, user: User):
    """Reprocess transcript for a conversation. Users can only reprocess their own conversations."""
    try:
        # Find the conversation using Beanie
        conversation_model = await Conversation.find_one(Conversation.conversation_id == conversation_id)
        if not conversation_model:
            return JSONResponse(status_code=404, content={"error": "Conversation not found"})

        # Check ownership for non-admin users
        if not user.is_superuser and conversation_model.user_id != str(user.user_id):
            return JSONResponse(status_code=403, content={"error": "Access forbidden. You can only reprocess your own conversations."})

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
                    "details": f"Conversation '{conversation_id}' exists but has no audio chunks in MongoDB"
                }
            )

        # Create new transcript version ID
        version_id = str(uuid.uuid4())

        # Enqueue job chain with RQ (transcription -> speaker recognition -> memory)
        from advanced_omi_backend.workers.transcription_jobs import (
            transcribe_full_audio_job,
        )

        # Job 1: Transcribe audio to text (reconstructs from MongoDB chunks)
        transcript_job = transcription_queue.enqueue(
            transcribe_full_audio_job,
            conversation_id,
            version_id,
            "reprocess",
            job_timeout=600,
            result_ttl=JOB_RESULT_TTL,
            job_id=f"reprocess_{conversation_id[:8]}",
            description=f"Transcribe audio for {conversation_id[:8]}",
            meta={'conversation_id': conversation_id}
        )
        logger.info(f"📥 RQ: Enqueued transcription job {transcript_job.id}")

        # Check if speaker recognition is enabled
        speaker_config = get_service_config('speaker_recognition')
        speaker_enabled = speaker_config.get('enabled', True)  # Default to True for backward compatibility

        # Job 2: Recognize speakers (conditional - only if enabled)
        speaker_dependency = transcript_job  # Start with transcription job
        speaker_job = None

        if speaker_enabled:
            speaker_job = transcription_queue.enqueue(
                recognise_speakers_job,
                conversation_id,
                version_id,
                depends_on=transcript_job,
                job_timeout=600,
                result_ttl=JOB_RESULT_TTL,
                job_id=f"speaker_{conversation_id[:8]}",
                description=f"Recognize speakers for {conversation_id[:8]}",
                meta={'conversation_id': conversation_id}
            )
            speaker_dependency = speaker_job  # Chain for next job
            logger.info(f"📥 RQ: Enqueued speaker recognition job {speaker_job.id} (depends on {transcript_job.id})")
        else:
            logger.info(f"⏭️  Speaker recognition disabled, skipping speaker job for conversation {conversation_id[:8]}")

        # Job 3: Extract memories
        # Depends on speaker job if it was created, otherwise depends on transcription
        # Note: redis_client is injected by @async_job decorator, don't pass it directly
        memory_job = memory_queue.enqueue(
            process_memory_job,
            conversation_id,
            depends_on=speaker_dependency,  # Either speaker_job or transcript_job
            job_timeout=1800,
            result_ttl=JOB_RESULT_TTL,
            job_id=f"memory_{conversation_id[:8]}",
            description=f"Extract memories for {conversation_id[:8]}",
            meta={'conversation_id': conversation_id}
        )
        if speaker_job:
            logger.info(f"📥 RQ: Enqueued memory job {memory_job.id} (depends on speaker job {speaker_job.id})")
        else:
            logger.info(f"📥 RQ: Enqueued memory job {memory_job.id} (depends on transcript job {transcript_job.id})")

        job = transcript_job  # For backward compatibility with return value
        logger.info(f"Created transcript reprocessing job {job.id} (version: {version_id}) for conversation {conversation_id}")

        return JSONResponse(content={
            "message": f"Transcript reprocessing started for conversation {conversation_id}",
            "job_id": job.id,
            "version_id": version_id,
            "status": "queued"
        })

    except Exception as e:
        logger.error(f"Error starting transcript reprocessing: {e}")
        return JSONResponse(status_code=500, content={"error": "Error starting transcript reprocessing"})


async def reprocess_memory(conversation_id: str, transcript_version_id: str, user: User):
    """Reprocess memory extraction for a specific transcript version. Users can only reprocess their own conversations."""
    try:
        # Find the conversation using Beanie
        conversation_model = await Conversation.find_one(Conversation.conversation_id == conversation_id)
        if not conversation_model:
            return JSONResponse(status_code=404, content={"error": "Conversation not found"})

        # Check ownership for non-admin users
        if not user.is_superuser and conversation_model.user_id != str(user.user_id):
            return JSONResponse(status_code=403, content={"error": "Access forbidden. You can only reprocess your own conversations."})

        # Resolve transcript version ID
        # Handle special "active" version ID
        if transcript_version_id == "active":
            active_version_id = conversation_model.active_transcript_version
            if not active_version_id:
                return JSONResponse(
                    status_code=404, content={"error": "No active transcript version found"}
                )
            transcript_version_id = active_version_id

        # Find the specific transcript version
        transcript_version = None
        for version in conversation_model.transcript_versions:
            if version.version_id == transcript_version_id:
                transcript_version = version
                break

        if not transcript_version:
            return JSONResponse(
                status_code=404, content={"error": f"Transcript version '{transcript_version_id}' not found"}
            )

        # Create new memory version ID
        version_id = str(uuid.uuid4())

        # Enqueue memory processing job with RQ (RQ handles job tracking)

        job = enqueue_memory_processing(
            client_id=conversation_model.client_id,
            user_id=str(user.user_id),
            user_email=user.email,
            conversation_id=conversation_id,
            priority=JobPriority.NORMAL
        )

        logger.info(f"Created memory reprocessing job {job.id} (version {version_id}) for conversation {conversation_id}")

        return JSONResponse(content={
            "message": f"Memory reprocessing started for conversation {conversation_id}",
            "job_id": job.id,
            "version_id": version_id,
            "transcript_version_id": transcript_version_id,
            "status": "queued"
        })

    except Exception as e:
        logger.error(f"Error starting memory reprocessing: {e}")
        return JSONResponse(status_code=500, content={"error": "Error starting memory reprocessing"})


async def activate_transcript_version(conversation_id: str, version_id: str, user: User):
    """Activate a specific transcript version. Users can only modify their own conversations."""
    try:
        # Find the conversation using Beanie
        conversation_model = await Conversation.find_one(Conversation.conversation_id == conversation_id)
        if not conversation_model:
            return JSONResponse(status_code=404, content={"error": "Conversation not found"})

        # Check ownership for non-admin users
        if not user.is_superuser and conversation_model.user_id != str(user.user_id):
            return JSONResponse(status_code=403, content={"error": "Access forbidden. You can only modify your own conversations."})

        # Activate the transcript version using Beanie model method
        success = conversation_model.set_active_transcript_version(version_id)
        if not success:
            return JSONResponse(
                status_code=400, content={"error": "Failed to activate transcript version"}
            )

        await conversation_model.save()

        # TODO: Trigger speaker recognition if configured
        # This would integrate with existing speaker recognition logic

        logger.info(f"Activated transcript version {version_id} for conversation {conversation_id} by user {user.user_id}")

        return JSONResponse(content={
            "message": f"Transcript version {version_id} activated successfully",
            "active_transcript_version": version_id
        })

    except Exception as e:
        logger.error(f"Error activating transcript version: {e}")
        return JSONResponse(status_code=500, content={"error": "Error activating transcript version"})


async def activate_memory_version(conversation_id: str, version_id: str, user: User):
    """Activate a specific memory version. Users can only modify their own conversations."""
    try:
        # Find the conversation using Beanie
        conversation_model = await Conversation.find_one(Conversation.conversation_id == conversation_id)
        if not conversation_model:
            return JSONResponse(status_code=404, content={"error": "Conversation not found"})

        # Check ownership for non-admin users
        if not user.is_superuser and conversation_model.user_id != str(user.user_id):
            return JSONResponse(status_code=403, content={"error": "Access forbidden. You can only modify your own conversations."})

        # Activate the memory version using Beanie model method
        success = conversation_model.set_active_memory_version(version_id)
        if not success:
            return JSONResponse(
                status_code=400, content={"error": "Failed to activate memory version"}
            )

        await conversation_model.save()

        logger.info(f"Activated memory version {version_id} for conversation {conversation_id} by user {user.user_id}")

        return JSONResponse(content={
            "message": f"Memory version {version_id} activated successfully",
            "active_memory_version": version_id
        })

    except Exception as e:
        logger.error(f"Error activating memory version: {e}")
        return JSONResponse(status_code=500, content={"error": "Error activating memory version"})


async def get_conversation_version_history(conversation_id: str, user: User):
    """Get version history for a conversation. Users can only access their own conversations."""
    try:
        # Find the conversation using Beanie to check ownership
        conversation_model = await Conversation.find_one(Conversation.conversation_id == conversation_id)
        if not conversation_model:
            return JSONResponse(status_code=404, content={"error": "Conversation not found"})

        # Check ownership for non-admin users
        if not user.is_superuser and conversation_model.user_id != str(user.user_id):
            return JSONResponse(status_code=403, content={"error": "Access forbidden. You can only access your own conversations."})

        # Get version history from model
        # Convert datetime objects to ISO strings for JSON serialization
        transcript_versions = []
        for v in conversation_model.transcript_versions:
            version_dict = v.model_dump()
            if version_dict.get('created_at'):
                version_dict['created_at'] = version_dict['created_at'].isoformat()
            transcript_versions.append(version_dict)

        memory_versions = []
        for v in conversation_model.memory_versions:
            version_dict = v.model_dump()
            if version_dict.get('created_at'):
                version_dict['created_at'] = version_dict['created_at'].isoformat()
            memory_versions.append(version_dict)

        history = {
            "conversation_id": conversation_id,
            "active_transcript_version": conversation_model.active_transcript_version,
            "active_memory_version": conversation_model.active_memory_version,
            "transcript_versions": transcript_versions,
            "memory_versions": memory_versions
        }

        return JSONResponse(content=history)

    except Exception as e:
        logger.error(f"Error fetching version history: {e}")
        return JSONResponse(status_code=500, content={"error": "Error fetching version history"})
