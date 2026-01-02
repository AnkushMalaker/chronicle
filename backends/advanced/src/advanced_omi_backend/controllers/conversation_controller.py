"""
Conversation controller for handling conversation-related business logic.
"""

import logging
import time
from pathlib import Path
from typing import Optional

from advanced_omi_backend.client_manager import (
    ClientManager,
    client_belongs_to_user,
)
from advanced_omi_backend.models.audio_file import AudioFile
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.users import User
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")

# Legacy audio_chunks collection is still used by some endpoints (speaker assignment, segment updates)
# But conversation queries now use the Conversation model directly
# Audio cropping operations are handled in audio_controller.py


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
        logger.exception(f"Error closing conversation for client {client_id}")
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
            "audio_uuid": conversation.audio_uuid,
            "user_id": conversation.user_id,
            "client_id": conversation.client_id,
            "audio_path": conversation.audio_path,
            "cropped_audio_path": conversation.cropped_audio_path,
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
        logger.exception(f"Error fetching conversation {conversation_id}")
        return JSONResponse(status_code=500, content={"error": "Error fetching conversation"})


async def get_conversations(user: User):
    """Get conversations with speech only (speech-driven architecture)."""
    try:
        # Build query based on user permissions using Beanie
        if not user.is_superuser:
            # Regular users can only see their own conversations
            user_conversations = await Conversation.find(
                Conversation.user_id == str(user.user_id)
            ).sort(-Conversation.created_at).to_list()
        else:
            # Admins see all conversations
            user_conversations = await Conversation.find_all().sort(-Conversation.created_at).to_list()

        # Build response with explicit curated fields - minimal for list view
        conversations = []
        for conv in user_conversations:
            conversations.append({
                "conversation_id": conv.conversation_id,
                "audio_uuid": conv.audio_uuid,
                "user_id": conv.user_id,
                "client_id": conv.client_id,
                "audio_path": conv.audio_path,
                "cropped_audio_path": conv.cropped_audio_path,
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


async def delete_conversation(conversation_id: str, user: User):
    """Delete a conversation and its associated audio files. Users can only delete their own conversations."""
    try:
        # Create masked identifier for logging
        masked_id = f"{conversation_id[:8]}...{conversation_id[-4:]}" if len(conversation_id) > 12 else "***"
        logger.info(f"Attempting to delete conversation: {masked_id}")

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

        # Get file paths before deletion
        audio_path = conversation.audio_path
        cropped_audio_path = conversation.cropped_audio_path
        audio_uuid = conversation.audio_uuid
        client_id = conversation.client_id

        # Delete the conversation from database
        await conversation.delete()
        logger.info(f"Deleted conversation {conversation_id}")

        # Also delete from legacy AudioFile collection if it exists (backward compatibility)
        audio_file = await AudioFile.find_one(AudioFile.audio_uuid == audio_uuid)
        if audio_file:
            await audio_file.delete()
            logger.info(f"Deleted legacy audio file record for {audio_uuid}")

        # Delete associated audio files from disk
        deleted_files = []
        if audio_path:
            try:
                # Construct full path to audio file
                full_audio_path = Path("/app/audio_chunks") / audio_path
                if full_audio_path.exists():
                    full_audio_path.unlink()
                    deleted_files.append(str(full_audio_path))
                    logger.info(f"Deleted audio file: {full_audio_path}")
            except Exception as e:
                logger.warning(f"Failed to delete audio file {audio_path}: {e}")

        if cropped_audio_path:
            try:
                # Construct full path to cropped audio file
                full_cropped_path = Path("/app/audio_chunks") / cropped_audio_path
                if full_cropped_path.exists():
                    full_cropped_path.unlink()
                    deleted_files.append(str(full_cropped_path))
                    logger.info(f"Deleted cropped audio file: {full_cropped_path}")
            except Exception as e:
                logger.warning(f"Failed to delete cropped audio file {cropped_audio_path}: {e}")

        logger.info(f"Successfully deleted conversation {conversation_id} for user {user.user_id}")

        # Prepare response message
        delete_summary = ["conversation"]
        if deleted_files:
            delete_summary.append(f"{len(deleted_files)} audio file(s)")

        return JSONResponse(
            status_code=200,
            content={
                "message": f"Successfully deleted {', '.join(delete_summary)} '{conversation_id}'",
                "deleted_files": deleted_files,
                "client_id": client_id,
                "conversation_id": conversation_id,
                "audio_uuid": audio_uuid
            }
        )

    except Exception as e:
        logger.exception(f"Error deleting conversation {conversation_id}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to delete conversation: {str(e)}"}
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

        # Get audio_uuid and file path from conversation
        audio_uuid = conversation_model.audio_uuid
        audio_path = conversation_model.audio_path

        if not audio_path:
            return JSONResponse(
                status_code=400, content={"error": "No audio file found for this conversation"}
            )

        # Check if file exists - try multiple possible locations
        possible_paths = [
            Path("/app/audio_chunks") / audio_path,
            Path(audio_path),  # fallback to relative path
        ]

        full_audio_path = None
        for path in possible_paths:
            if path.exists():
                full_audio_path = path
                break

        if not full_audio_path:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "Audio file not found on disk",
                    "details": f"Conversation exists but audio file '{audio_path}' is missing from expected locations",
                    "searched_paths": [str(p) for p in possible_paths]
                }
            )

        # Create new transcript version ID
        import uuid
        version_id = str(uuid.uuid4())

        # Enqueue job chain with RQ (transcription -> speaker recognition -> cropping -> memory)
        from advanced_omi_backend.workers.transcription_jobs import transcribe_full_audio_job
        from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job
        from advanced_omi_backend.workers.audio_jobs import process_cropping_job
        from advanced_omi_backend.workers.memory_jobs import process_memory_job
        from advanced_omi_backend.controllers.queue_controller import transcription_queue, memory_queue, default_queue, JOB_RESULT_TTL

        # Job 1: Transcribe audio to text
        transcript_job = transcription_queue.enqueue(
            transcribe_full_audio_job,
            conversation_id,
            audio_uuid,
            str(full_audio_path),
            version_id,
            "reprocess",
            job_timeout=600,
            result_ttl=JOB_RESULT_TTL,
            job_id=f"reprocess_{conversation_id[:8]}",
            description=f"Transcribe audio for {conversation_id[:8]}",
            meta={'audio_uuid': audio_uuid, 'conversation_id': conversation_id}
        )
        logger.info(f"📥 RQ: Enqueued transcription job {transcript_job.id}")

        # Job 2: Recognize speakers (depends on transcription)
        speaker_job = transcription_queue.enqueue(
            recognise_speakers_job,
            conversation_id,
            version_id,
            str(full_audio_path),
            "",  # transcript_text - will be read from DB
            [],  # words - will be read from DB
            depends_on=transcript_job,
            job_timeout=600,
            result_ttl=JOB_RESULT_TTL,
            job_id=f"speaker_{conversation_id[:8]}",
            description=f"Recognize speakers for {conversation_id[:8]}",
            meta={'audio_uuid': audio_uuid, 'conversation_id': conversation_id}
        )
        logger.info(f"📥 RQ: Enqueued speaker recognition job {speaker_job.id} (depends on {transcript_job.id})")

        # Job 3: Audio cropping (depends on speaker recognition)
        cropping_job = default_queue.enqueue(
            process_cropping_job,
            conversation_id,
            str(full_audio_path),
            depends_on=speaker_job,
            job_timeout=300,
            result_ttl=JOB_RESULT_TTL,
            job_id=f"crop_{conversation_id[:8]}",
            description=f"Crop audio for {conversation_id[:8]}",
            meta={'audio_uuid': audio_uuid, 'conversation_id': conversation_id}
        )
        logger.info(f"📥 RQ: Enqueued audio cropping job {cropping_job.id} (depends on {speaker_job.id})")

        # Job 4: Extract memories (depends on cropping)
        # Note: redis_client is injected by @async_job decorator, don't pass it directly
        memory_job = memory_queue.enqueue(
            process_memory_job,
            conversation_id,
            depends_on=cropping_job,
            job_timeout=1800,
            result_ttl=JOB_RESULT_TTL,
            job_id=f"memory_{conversation_id[:8]}",
            description=f"Extract memories for {conversation_id[:8]}",
            meta={'audio_uuid': audio_uuid, 'conversation_id': conversation_id}
        )
        logger.info(f"📥 RQ: Enqueued memory job {memory_job.id} (depends on {cropping_job.id})")

        job = transcript_job  # For backward compatibility with return value
        logger.info(f"Created transcript reprocessing job {job.id} (version: {version_id}) for conversation {conversation_id}")

        return JSONResponse(content={
            "message": f"Transcript reprocessing started for conversation {conversation_id}",
            "job_id": job.id,
            "version_id": version_id,
            "status": "queued"
        })

    except Exception as e:
        logger.exception(f"Error starting transcript reprocessing")
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
        import uuid
        version_id = str(uuid.uuid4())

        # Enqueue memory processing job with RQ (RQ handles job tracking)
        from advanced_omi_backend.workers.memory_jobs import enqueue_memory_processing
        from advanced_omi_backend.models.job import JobPriority

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
        logger.exception(f"Error starting memory reprocessing")
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
        logger.exception(f"Error activating transcript version")
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
        logger.exception(f"Error activating memory version")
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
        logger.exception(f"Error fetching version history")
        return JSONResponse(status_code=500, content={"error": "Error fetching version history"})
