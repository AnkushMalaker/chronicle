"""
Memory-related RQ job functions.

This module contains jobs related to memory extraction and processing.
"""

import logging
import time
import uuid
from typing import Any, Dict

from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    memory_queue,
)
from advanced_omi_backend.models.job import JobPriority, async_job
from advanced_omi_backend.services.plugin_service import ensure_plugin_router

logger = logging.getLogger(__name__)

MIN_CONVERSATION_LENGTH = 10


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

    # Extract conversation text and speakers from transcript segments in a single pass
    dialogue_lines = []
    transcript_speakers = set()
    segments = conversation_model.segments
    if segments:
        for segment in segments:
            text = segment.text.strip()
            speaker = segment.speaker
            if text:
                dialogue_lines.append(f"{speaker}: {text}")
            if speaker and speaker != "Unknown":
                transcript_speakers.add(speaker.strip().lower())
    full_conversation = "\n".join(dialogue_lines)

    # Fallback: if segments have no text content but transcript exists, use transcript
    # This handles cases where speaker recognition fails/is disabled
    if (
        len(full_conversation) < MIN_CONVERSATION_LENGTH
        and conversation_model.transcript
        and isinstance(conversation_model.transcript, str)
    ):
        logger.info(
            f"Segments empty or too short, falling back to transcript text for {conversation_id}"
        )
        full_conversation = conversation_model.transcript

    if len(full_conversation) < MIN_CONVERSATION_LENGTH:
        logger.warning(f"Conversation too short for memory processing: {conversation_id}")
        return {"success": False, "error": "Conversation too short"}

    # Check primary speakers filter (reuse `user` from above — no duplicate DB call)
    if user and user.primary_speakers:
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

        if success:
            processing_time = time.time() - start_time

            # Determine memory provider from memory service
            memory_provider = memory_service.provider_identifier

            # Only create memory version if new memories were created
            if created_memory_ids:
                # Add memory version to conversation
                conversation_model = await Conversation.find_one(
                    Conversation.conversation_id == conversation_id
                )
                if conversation_model:
                    # Get active transcript version for reference
                    transcript_version_id = (
                        conversation_model.active_transcript_version or "unknown"
                    )

                    # Create version ID for this memory extraction
                    version_id = str(uuid.uuid4())

                    # Add memory version with metadata
                    conversation_model.add_memory_version(
                        version_id=version_id,
                        memory_count=len(created_memory_ids),
                        transcript_version_id=transcript_version_id,
                        provider=(
                            conversation_model.MemoryProvider.OPENMEMORY_MCP
                            if memory_provider == "openmemory_mcp"
                            else conversation_model.MemoryProvider.CHRONICLE
                        ),
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
                                memory_details.append(
                                    {"memory_id": memory_id, "text": memory_entry.content[:200]}
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
            else:
                logger.info(
                    f"ℹ️ Memory processing completed for conversation {conversation_id} - no new memories created (deduplication) in {processing_time:.2f}s"
                )

            # NOTE: Listening jobs are restarted by open_conversation_job (not here)
            # This allows users to resume talking immediately after conversation closes,
            # without waiting for memory processing to complete.

            # Extract entities and relationships to knowledge graph (if enabled)
            try:
                from advanced_omi_backend.model_registry import get_config

                config = get_config()
                kg_enabled = (
                    config.get("memory", {}).get("knowledge_graph", {}).get("enabled", False)
                )

                if kg_enabled:
                    from advanced_omi_backend.services.knowledge_graph import (
                        get_knowledge_graph_service,
                    )

                    kg_service = get_knowledge_graph_service()
                    kg_result = await kg_service.process_conversation(
                        conversation_id=conversation_id,
                        transcript=full_conversation,
                        user_id=user_id,
                        conversation_name=(
                            conversation_model.title
                            if hasattr(conversation_model, "title")
                            else None
                        ),
                    )
                    if kg_result.get("entities", 0) > 0:
                        logger.info(
                            f"🔗 Knowledge graph: extracted {kg_result.get('entities', 0)} entities, "
                            f"{kg_result.get('relationships', 0)} relationships, "
                            f"{kg_result.get('promises', 0)} promises from {conversation_id}"
                        )
                else:
                    logger.debug("Knowledge graph extraction disabled in config")
            except Exception as e:
                # Knowledge graph extraction is optional - don't fail the job
                logger.warning(f"⚠️ Knowledge graph extraction failed (non-fatal): {e}")

            # Trigger memory-level plugins (ALWAYS dispatch when success, even with 0 new memories)
            try:
                plugin_router = await ensure_plugin_router()

                if plugin_router:
                    plugin_data = {
                        "memories": created_memory_ids or [],
                        "conversation": {
                            "conversation_id": conversation_id,
                            "client_id": client_id,
                            "user_id": user_id,
                            "user_email": user_email,
                        },
                        "memory_count": len(created_memory_ids) if created_memory_ids else 0,
                        "conversation_id": conversation_id,
                    }

                    logger.info(
                        f"🔌 DISPATCH: memory.processed event "
                        f"(conversation={conversation_id[:12]}, memories={len(created_memory_ids) if created_memory_ids else 0})"
                    )

                    plugin_results = await plugin_router.dispatch_event(
                        event="memory.processed",
                        user_id=user_id,
                        data=plugin_data,
                        metadata={
                            "processing_time": processing_time,
                            "memory_provider": memory_provider,
                        },
                    )

                    logger.info(
                        f"🔌 RESULT: memory.processed dispatched to {len(plugin_results) if plugin_results else 0} plugins"
                    )

                    if plugin_results:
                        logger.info(f"📌 Triggered {len(plugin_results)} memory-level plugins")
                        for result in plugin_results:
                            if result.message:
                                logger.info(f"  Plugin result: {result.message}")

            except Exception as e:
                logger.warning(f"⚠️ Error triggering memory-level plugins: {e}")

            return {
                "success": True,
                "memories_created": len(created_memory_ids) if created_memory_ids else 0,
                "processing_time": processing_time,
            }
        else:
            # Memory extraction failed
            return {"success": False, "error": "Memory extraction returned failure"}
    else:
        return {"success": False, "error": "Memory service returned False"}


def enqueue_memory_processing(
    conversation_id: str,
    priority: JobPriority = JobPriority.NORMAL,
):
    """
    Enqueue a memory processing job.

    The job fetches all needed data (client_id, user_id, user_email) from the
    conversation document internally, so only conversation_id is needed.

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
