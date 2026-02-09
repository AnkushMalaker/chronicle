"""
Cron job implementations for the Chronicle scheduler.

Jobs:
  - speaker_finetuning: sends applied diarization annotations to speaker service
  - asr_jargon_extraction: extracts jargon from recent memories, caches in Redis
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from advanced_omi_backend.llm_client import async_generate
from advanced_omi_backend.prompt_registry import get_prompt_registry

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# TTL for cached jargon: 2 hours (job runs every 30 min, so always refreshed)
JARGON_CACHE_TTL = 7200

# Maximum number of recent memories to pull per user
MAX_RECENT_MEMORIES = 50

# How far back to look for memories (24 hours in seconds)
MEMORY_LOOKBACK_SECONDS = 86400


# ---------------------------------------------------------------------------
# Job 1: Speaker Fine-tuning
# ---------------------------------------------------------------------------

async def run_speaker_finetuning_job() -> dict:
    """Process applied diarization annotations and send to speaker recognition service.

    This mirrors the logic in ``finetuning_routes.process_annotations_for_training``
    but is invocable from the cron scheduler without an HTTP request.
    """
    from advanced_omi_backend.models.annotation import Annotation, AnnotationType
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
    from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_audio_segment

    # Find annotations ready for training
    annotations = await Annotation.find(
        Annotation.annotation_type == AnnotationType.DIARIZATION,
        Annotation.processed == True,
    ).to_list()

    ready_for_training = [
        a for a in annotations if not a.processed_by or "training" not in a.processed_by
    ]

    if not ready_for_training:
        logger.info("Speaker finetuning: no annotations ready for training")
        return {"processed": 0, "message": "No annotations ready for training"}

    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        logger.warning("Speaker finetuning: speaker recognition service is not enabled")
        return {"processed": 0, "message": "Speaker recognition service not enabled"}

    enrolled = 0
    appended = 0
    failed = 0
    cleaned = 0

    for annotation in ready_for_training:
        try:
            conversation = await Conversation.find_one(
                Conversation.conversation_id == annotation.conversation_id
            )
            if not conversation or not conversation.active_transcript:
                logger.warning(
                    f"Conversation {annotation.conversation_id} not found — "
                    f"deleting orphaned annotation {annotation.id}"
                )
                await annotation.delete()
                cleaned += 1
                continue

            if annotation.segment_index >= len(conversation.active_transcript.segments):
                logger.warning(
                    f"Invalid segment index {annotation.segment_index} for "
                    f"conversation {annotation.conversation_id} — "
                    f"deleting orphaned annotation {annotation.id}"
                )
                await annotation.delete()
                cleaned += 1
                continue

            segment = conversation.active_transcript.segments[annotation.segment_index]

            wav_bytes = await reconstruct_audio_segment(
                conversation_id=annotation.conversation_id,
                start_time=segment.start,
                end_time=segment.end,
            )
            if not wav_bytes:
                failed += 1
                continue

            # Intentional: only single admin user (user_id=1) is supported currently
            existing_speaker = await speaker_client.get_speaker_by_name(
                speaker_name=annotation.corrected_speaker,
                user_id=1,
            )

            if existing_speaker:
                result = await speaker_client.append_to_speaker(
                    speaker_id=existing_speaker["id"], audio_data=wav_bytes
                )
                if "error" in result:
                    failed += 1
                    continue
                appended += 1
            else:
                result = await speaker_client.enroll_new_speaker(
                    speaker_name=annotation.corrected_speaker,
                    audio_data=wav_bytes,
                    user_id=1,
                )
                if "error" in result:
                    failed += 1
                    continue
                enrolled += 1

            # Mark as trained
            annotation.processed_by = (
                f"{annotation.processed_by},training" if annotation.processed_by else "training"
            )
            annotation.updated_at = datetime.now(timezone.utc)
            await annotation.save()

        except Exception as e:
            logger.error(f"Speaker finetuning: error processing annotation {annotation.id}: {e}")
            failed += 1

    total = enrolled + appended
    logger.info(
        f"Speaker finetuning complete: {total} processed "
        f"({enrolled} new, {appended} appended, {failed} failed, {cleaned} orphaned cleaned)"
    )
    return {"enrolled": enrolled, "appended": appended, "failed": failed, "cleaned": cleaned, "processed": total}


# ---------------------------------------------------------------------------
# Job 2: ASR Jargon Extraction
# ---------------------------------------------------------------------------

async def run_asr_jargon_extraction_job() -> dict:
    """Extract jargon from recent memories for all users and cache in Redis."""
    from advanced_omi_backend.models.user import User

    users = await User.find_all().to_list()
    processed = 0
    skipped = 0
    errors = 0

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        for user in users:
            user_id = str(user.id)
            try:
                jargon = await _extract_jargon_for_user(user_id)
                if jargon:
                    await redis_client.set(f"asr:jargon:{user_id}", jargon, ex=JARGON_CACHE_TTL)
                    processed += 1
                    logger.debug(f"Cached jargon for user {user_id}: {jargon[:80]}...")
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f"Jargon extraction failed for user {user_id}: {e}")
                errors += 1
    finally:
        await redis_client.close()

    logger.info(
        f"ASR jargon extraction complete: {processed} users processed, "
        f"{skipped} skipped, {errors} errors"
    )
    return {"users_processed": processed, "skipped": skipped, "errors": errors}


async def _extract_jargon_for_user(user_id: str) -> Optional[str]:
    """Pull recent memories from Qdrant, call LLM to extract jargon terms.

    Returns a comma-separated string of jargon terms, or None if nothing found.
    """
    from advanced_omi_backend.services.memory import get_memory_service
    from advanced_omi_backend.services.memory.providers.chronicle import MemoryService

    memory_service = get_memory_service()

    # Only works with Chronicle provider (has Qdrant vector store)
    if not isinstance(memory_service, MemoryService):
        logger.debug("Jargon extraction requires Chronicle memory provider, skipping")
        return None

    if memory_service.vector_store is None:
        return None

    since_ts = int(time.time()) - MEMORY_LOOKBACK_SECONDS

    memories = await memory_service.vector_store.get_recent_memories(
        user_id=user_id,
        since_timestamp=since_ts,
        limit=MAX_RECENT_MEMORIES,
    )

    if not memories:
        return None

    # Concatenate memory content
    memory_text = "\n".join(m.content for m in memories if m.content)
    if not memory_text.strip():
        return None

    # Use LLM to extract jargon
    registry = get_prompt_registry()
    prompt_template = await registry.get_prompt("asr.jargon_extraction", memories=memory_text)

    result = await async_generate(prompt_template)

    # Clean up: strip whitespace, remove empty items
    if result:
        terms = [t.strip() for t in result.split(",") if t.strip()]
        if terms:
            return ", ".join(terms)

    return None
