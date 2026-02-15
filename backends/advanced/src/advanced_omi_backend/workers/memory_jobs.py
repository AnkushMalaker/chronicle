"""
Memory-related RQ job functions.

This module contains jobs related to memory extraction and processing.

Supports two processing pathways:
1. **Normal extraction**: Extracts fresh facts from transcript, deduplicates
   against existing user memories, and proposes ADD/UPDATE/DELETE actions.
2. **Speaker reprocess**: When triggered after speaker re-identification,
   computes a diff between old and new speaker labels, fetches existing
   conversation-specific memories, and asks the LLM to make targeted
   corrections to speaker attribution in those memories.
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    memory_queue,
)
from advanced_omi_backend.models.job import JobPriority, async_job
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.services.plugin_service import ensure_plugin_router

logger = logging.getLogger(__name__)

MIN_CONVERSATION_LENGTH = 10


def compute_speaker_diff(
    old_segments: list,
    new_segments: list,
) -> List[Dict[str, Any]]:
    """Compare old and new transcript segments to identify speaker changes.

    Matches segments by time overlap and detects where speaker labels differ.

    Args:
        old_segments: Segments from the previous transcript version
        new_segments: Segments from the new (active) transcript version

    Returns:
        List of change dicts, each with keys:
        - ``type``: "speaker_change", "text_change", or "new_segment"
        - ``text``: The segment text
        - ``old_speaker`` / ``new_speaker``: For speaker changes
        - ``old_text`` / ``new_text``: For text changes
        - ``start`` / ``end``: Time boundaries
    """
    changes: List[Dict[str, Any]] = []

    for new_seg in new_segments:
        new_start = new_seg.start
        new_end = new_seg.end

        # Find best matching old segment by time overlap
        best_match = None
        best_overlap = 0.0

        for old_seg in old_segments:
            overlap_start = max(old_seg.start, new_start)
            overlap_end = min(old_seg.end, new_end)
            overlap = max(0.0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_match = old_seg

        if best_match:
            # Check for speaker change
            if best_match.speaker != new_seg.speaker:
                changes.append(
                    {
                        "type": "speaker_change",
                        "text": new_seg.text.strip(),
                        "old_speaker": best_match.speaker,
                        "new_speaker": new_seg.speaker,
                        "start": new_start,
                        "end": new_end,
                    }
                )
            # Check for text change (less common in speaker reprocessing)
            if best_match.text.strip() != new_seg.text.strip():
                changes.append(
                    {
                        "type": "text_change",
                        "old_text": best_match.text.strip(),
                        "new_text": new_seg.text.strip(),
                        "speaker": new_seg.speaker,
                        "start": new_start,
                        "end": new_end,
                    }
                )
        else:
            # No matching old segment found
            changes.append(
                {
                    "type": "new_segment",
                    "text": new_seg.text.strip(),
                    "speaker": new_seg.speaker,
                    "start": new_start,
                    "end": new_end,
                }
            )

    return changes


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
            seg_type = getattr(segment, 'segment_type', 'speech')
            if text:
                if seg_type == "event":
                    # Non-speech event: include as context marker without speaker prefix
                    dialogue_lines.append(f"[{text}]" if not text.startswith("[") else text)
                elif seg_type == "note":
                    # User-inserted note: include as distinct context
                    dialogue_lines.append(f"[Note: {text}]")
                else:
                    # Normal speech segment
                    dialogue_lines.append(f"{speaker}: {text}")
            if speaker and speaker != "Unknown" and seg_type == "speech":
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

    # Detect reprocess trigger from RQ job metadata
    from rq import get_current_job as _get_current_job

    current_rq_job = _get_current_job()
    trigger = (
        current_rq_job.meta.get("trigger")
        if current_rq_job and current_rq_job.meta
        else None
    )

    # Process memory — choose pathway based on trigger
    memory_service = get_memory_service()

    if trigger == "reprocess_after_speaker":
        # === Speaker reprocess pathway ===
        # Compute diff between old and new transcript versions
        memory_result = await _process_speaker_reprocess(
            memory_service=memory_service,
            conversation_model=conversation_model,
            full_conversation=full_conversation,
            client_id=client_id,
            conversation_id=conversation_id,
            user_id=user_id,
            user_email=user_email,
        )
    else:
        # === Normal extraction pathway ===
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
                        event=PluginEvent.MEMORY_PROCESSED,
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


async def _process_speaker_reprocess(
    memory_service,
    conversation_model,
    full_conversation: str,
    client_id: str,
    conversation_id: str,
    user_id: str,
    user_email: str,
):
    """Handle memory reprocessing after speaker re-identification.

    Computes the diff between the previous and current transcript versions
    (specifically speaker label changes), then delegates to the memory
    service's ``reprocess_memory`` method for targeted updates.

    Falls back to normal ``add_memory`` if diff computation fails or
    no meaningful changes are detected.

    Args:
        memory_service: Active memory service instance
        conversation_model: Conversation Beanie document
        full_conversation: New transcript as dialogue lines
        client_id: Client identifier
        conversation_id: Conversation identifier
        user_id: User identifier
        user_email: User email

    Returns:
        Tuple of (success, memory_ids) matching ``add_memory`` return type
    """
    active_version = conversation_model.active_transcript

    if not active_version:
        logger.warning(
            f"🔄 Reprocess: no active transcript version for {conversation_id}, "
            f"falling back to normal extraction"
        )
        return await memory_service.add_memory(
            full_conversation, client_id, conversation_id, user_id, user_email,
            allow_update=True,
        )

    # Find the source (previous) transcript version from metadata
    source_version_id = active_version.metadata.get("source_version_id")

    if not source_version_id:
        logger.warning(
            f"🔄 Reprocess: no source_version_id in active transcript metadata "
            f"for {conversation_id}, falling back to normal extraction"
        )
        return await memory_service.add_memory(
            full_conversation, client_id, conversation_id, user_id, user_email,
            allow_update=True,
        )

    # Find the source version's segments
    source_version = None
    for v in conversation_model.transcript_versions:
        if v.version_id == source_version_id:
            source_version = v
            break

    if not source_version or not source_version.segments:
        logger.warning(
            f"🔄 Reprocess: source version {source_version_id} not found or has no segments "
            f"for {conversation_id}, falling back to normal extraction"
        )
        return await memory_service.add_memory(
            full_conversation, client_id, conversation_id, user_id, user_email,
            allow_update=True,
        )

    # Compute the speaker diff
    transcript_diff = compute_speaker_diff(
        source_version.segments,
        active_version.segments,
    )

    if not transcript_diff:
        logger.info(
            f"🔄 Reprocess: no speaker changes detected between versions "
            f"for {conversation_id}, falling back to normal extraction"
        )
        return await memory_service.add_memory(
            full_conversation, client_id, conversation_id, user_id, user_email,
            allow_update=True,
        )

    # Build the previous transcript for context
    previous_lines = []
    for seg in source_version.segments:
        text = seg.text.strip()
        if text:
            previous_lines.append(f"{seg.speaker}: {text}")
    previous_transcript = "\n".join(previous_lines)

    logger.info(
        f"🔄 Reprocess: detected {len(transcript_diff)} changes "
        f"(speakers reprocessed) for {conversation_id}"
    )

    # Use the reprocess pathway
    return await memory_service.reprocess_memory(
        transcript=full_conversation,
        client_id=client_id,
        source_id=conversation_id,
        user_id=user_id,
        user_email=user_email,
        transcript_diff=transcript_diff,
        previous_transcript=previous_transcript,
    )


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
