"""
Memory-related RQ job functions.

This module contains jobs related to memory extraction and processing.
"""

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Dict

from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    memory_queue,
)
from advanced_omi_backend.models.job import BaseRQJob, JobPriority, async_job
from advanced_omi_backend.services.memory.base import MemoryEntry

logger = logging.getLogger(__name__)


@async_job(redis=True, beanie=True)
async def process_memory_job(conversation_id: str, *, redis_client=None) -> Dict[str, Any]:
    """
    RQ job function for memory extraction and processing from conversations.

    V2 Architecture:
        1. Extracts memories from conversation transcript
        2. Checks primary speakers filter if configured
        3. Uses configured memory provider (chronicle or openmemory_mcp)
        4. Stores memory references in conversation document

    Note: Listening jobs are restarted by open_conversation_job (not here).
    This allows users to resume talking immediately after conversation closes,
    without waiting for memory processing to complete.

    Args:
        conversation_id: Conversation ID to process
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with processing results
    """
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.services.memory import get_memory_service
    from advanced_omi_backend.users import get_user_by_id

    start_time = time.time()
    logger.info(f"🔄 Starting memory processing for conversation {conversation_id}")

    # Get conversation data
    conversation_model = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation_model:
        logger.warning(f"No conversation found for {conversation_id}")
        return {"success": False, "error": "Conversation not found"}

    # Get client_id, user_id, and user_email from conversation/user
    client_id = conversation_model.client_id
    user_id = conversation_model.user_id

    user = await get_user_by_id(user_id)
    if user:
        user_email = user.email
    else:
        logger.warning(f"Could not find user {user_id}")
        user_email = ""

    logger.info(
        f"🔄 Processing memory for conversation {conversation_id}, client={client_id}, user={user_id}"
    )

    # Extract conversation text from transcript segments
    full_conversation = ""
    segments = conversation_model.segments
    if segments:
        dialogue_lines = []
        for segment in segments:
            # Handle both dict and object segments
            if isinstance(segment, dict):
                text = segment.get("text", "").strip()
                speaker = segment.get("speaker", "Unknown")
            else:
                text = getattr(segment, "text", "").strip()
                speaker = getattr(segment, "speaker", "Unknown")

            if text:
                dialogue_lines.append(f"{speaker}: {text}")
        full_conversation = "\n".join(dialogue_lines)

    # Fallback: if segments have no text content but transcript exists, use transcript
    # This handles cases where speaker recognition fails/is disabled
    if len(full_conversation) < 10 and conversation_model.transcript and isinstance(conversation_model.transcript, str):
        logger.info(f"Segments empty or too short, falling back to transcript text for {conversation_id}")
        full_conversation = conversation_model.transcript

    if len(full_conversation) < 10:
        logger.warning(f"Conversation too short for memory processing: {conversation_id}")
        return {"success": False, "error": "Conversation too short"}

    # Check primary speakers filter
    user = await get_user_by_id(user_id)
    if user and user.primary_speakers:
        transcript_speakers = set()
        for segment in conversation_model.segments:
            # Handle both dict and object segments
            if isinstance(segment, dict):
                identified_as = segment.get("identified_as")
            else:
                identified_as = getattr(segment, "identified_as", None)

            if identified_as and identified_as != "Unknown":
                transcript_speakers.add(identified_as.strip().lower())

        primary_speaker_names = {ps["name"].strip().lower() for ps in user.primary_speakers}

        if transcript_speakers and not transcript_speakers.intersection(primary_speaker_names):
            logger.info(
                f"Skipping memory - no primary speakers found in conversation {conversation_id}"
            )
            return {"success": True, "skipped": True, "reason": "No primary speakers"}

    # Process memory
    memory_service = get_memory_service()
    memory_result = await memory_service.add_memory(
        full_conversation,
        client_id,
        conversation_id,
        user_id,
        user_email,
        allow_update=True,
    )

    if memory_result:
        success, created_memory_ids = memory_result

        if success and created_memory_ids:
            # Add memory version to conversation
            conversation_model = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            if conversation_model:
                processing_time = time.time() - start_time

                # Get active transcript version for reference
                transcript_version_id = conversation_model.active_transcript_version or "unknown"

                # Determine memory provider from memory service
                memory_provider = conversation_model.MemoryProvider.CHRONICLE  # Default
                try:
                    memory_service_obj = get_memory_service()
                    provider_name = memory_service_obj.__class__.__name__
                    if "OpenMemory" in provider_name:
                        memory_provider = conversation_model.MemoryProvider.OPENMEMORY_MCP
                except Exception:
                    pass

                # Create version ID for this memory extraction
                version_id = str(uuid.uuid4())

                # Add memory version with metadata
                conversation_model.add_memory_version(
                    version_id=version_id,
                    memory_count=len(created_memory_ids),
                    transcript_version_id=transcript_version_id,
                    provider=memory_provider,
                    processing_time_seconds=processing_time,
                    metadata={"memory_ids": created_memory_ids},
                    set_as_active=True,
                )
                await conversation_model.save()

            logger.info(
                f"✅ Completed memory processing for conversation {conversation_id} - created {len(created_memory_ids)} memories in {processing_time:.2f}s"
            )

            # Update job metadata with memory information
            from rq import get_current_job

            current_job = get_current_job()
            if current_job:
                if not current_job.meta:
                    current_job.meta = {}

                # Fetch memory details to display in UI
                memory_details = []
                try:
                    for memory_id in created_memory_ids[:5]:  # Limit to first 5 for display
                        memory_entry = await memory_service.get_memory(memory_id, user_id)
                        if memory_entry:
                            # Handle different return types from memory service
                            memory_text: str
                            if isinstance(memory_entry, MemoryEntry):
                                # MemoryEntry object with content attribute
                                memory_text = memory_entry.content
                            elif isinstance(memory_entry, dict):
                                # Dictionary with "content" key
                                if "content" in memory_entry:
                                    memory_text = memory_entry["content"]
                                else:
                                    logger.error(
                                        f"Dict memory entry missing 'content' key for {memory_id}: {list(memory_entry.keys())}"
                                    )
                                    raise ValueError(
                                        f"Dict memory entry missing 'content' key for memory {memory_id}"
                                    )
                            elif isinstance(memory_entry, str):
                                # String content directly
                                memory_text = memory_entry
                            else:
                                # Unexpected type
                                logger.error(
                                    f"Unexpected memory entry type for {memory_id}: {type(memory_entry).__name__}"
                                )
                                raise TypeError(
                                    f"Unexpected memory entry type: {type(memory_entry).__name__}"
                                )

                            # Truncate to 200 chars
                            memory_details.append(
                                {"memory_id": memory_id, "text": memory_text[:200]}
                            )
                except Exception as e:
                    logger.warning(f"Failed to fetch memory details for UI: {e}")

                current_job.meta.update(
                    {
                        "conversation_id": conversation_id,
                        "memories_created": len(created_memory_ids),
                        "memory_ids": created_memory_ids[:5],  # Store first 5 IDs
                        "memory_details": memory_details,
                        "processing_time": processing_time,
                    }
                )
                current_job.save_meta()

            # NOTE: Listening jobs are restarted by open_conversation_job (not here)
            # This allows users to resume talking immediately after conversation closes,
            # without waiting for memory processing to complete.

            return {
                "success": True,
                "memories_created": len(created_memory_ids),
                "processing_time": processing_time,
            }
        else:
            # No memories created - still successful
            return {"success": True, "memories_created": 0, "skipped": True}
    else:
        return {"success": False, "error": "Memory service returned False"}


def enqueue_memory_processing(
    client_id: str,
    user_id: str,
    user_email: str,
    conversation_id: str,
    priority: JobPriority = JobPriority.NORMAL,
):
    """
    Enqueue a memory processing job.

    Returns RQ Job object for tracking.
    """
    timeout_mapping = {
        JobPriority.URGENT: 3600,  # 60 minutes
        JobPriority.HIGH: 2400,  # 40 minutes
        JobPriority.NORMAL: 1800,  # 30 minutes
        JobPriority.LOW: 900,  # 15 minutes
    }

    job = memory_queue.enqueue(
        process_memory_job,
        conversation_id,  # Only argument needed - job fetches conversation data internally
        job_timeout=timeout_mapping.get(priority, 1800),
        result_ttl=JOB_RESULT_TTL,
        job_id=f"memory_{conversation_id[:8]}",
        description=f"Process memory for conversation {conversation_id[:8]}",
    )

    logger.info(f"📥 RQ: Enqueued memory job {job.id} for conversation {conversation_id}")
    return job
