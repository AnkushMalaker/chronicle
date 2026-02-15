"""
Cron job implementations for the Chronicle scheduler.

Jobs:
  - speaker_finetuning: sends applied diarization annotations to speaker service
  - asr_finetuning: exports annotated conversations to VibeVoice ASR for LoRA fine-tuning
  - asr_jargon_extraction: extracts jargon from recent memories, caches in Redis
"""

import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
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
# Job 2: ASR Fine-tuning (VibeVoice LoRA)
# ---------------------------------------------------------------------------

_ASR_TRAINING_MARKER = "asr_training"


def _build_vibevoice_label(conversation) -> dict:
    """Convert Chronicle conversation to VibeVoice training label format.

    Maps SpeakerSegment data to the JSON structure expected by VibeVoice's
    LoRA fine-tuning scripts: speaker ints, timestamped segments with text.
    """
    transcript = conversation.active_transcript
    if not transcript:
        return {}

    speaker_map: dict[str, int] = {}
    segments = []
    for seg in transcript.segments:
        speaker_id = speaker_map.setdefault(seg.speaker, len(speaker_map))
        segments.append({
            "speaker": speaker_id,
            "text": seg.text,
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
        })

    return {
        "audio_path": f"{conversation.conversation_id}.wav",
        "audio_duration": conversation.audio_total_duration,
        "segments": segments,
    }


async def run_asr_finetuning_job() -> dict:
    """Export annotated conversations to VibeVoice ASR service for LoRA fine-tuning.

    Finds transcript and diarization annotations that have been applied but not
    yet consumed by ASR training. Groups by conversation, reconstructs WAV audio,
    builds VibeVoice training labels, and POSTs to the ASR service's /fine-tune endpoint.
    """
    from advanced_omi_backend.model_registry import get_models_registry
    from advanced_omi_backend.models.annotation import Annotation, AnnotationType
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_wav_from_conversation

    # Resolve STT service URL from model registry (same URL used for transcription)
    registry = get_models_registry()
    stt_model = registry.get_default("stt") if registry else None
    if not stt_model or not stt_model.model_url:
        logger.warning("ASR finetuning: no STT model configured in registry, skipping")
        return {"conversations_exported": 0, "annotations_consumed": 0, "message": "No STT model configured"}

    vibevoice_url = stt_model.model_url.rstrip("/")

    # Find applied annotations (TRANSCRIPT and DIARIZATION) not yet consumed by ASR training
    annotations = await Annotation.find(
        {"annotation_type": {"$in": [AnnotationType.TRANSCRIPT.value, AnnotationType.DIARIZATION.value]}},
        Annotation.processed == True,
    ).to_list()

    ready = [
        a for a in annotations
        if not a.processed_by or _ASR_TRAINING_MARKER not in a.processed_by
    ]

    if not ready:
        logger.info("ASR finetuning: no annotations ready for export")
        return {"conversations_exported": 0, "annotations_consumed": 0, "message": "No annotations ready"}

    # Group annotations by conversation_id
    by_conversation: dict[str, list[Annotation]] = {}
    for a in ready:
        if a.conversation_id:
            by_conversation.setdefault(a.conversation_id, []).append(a)

    exported = 0
    consumed = 0
    errors = 0

    # Optionally load cached jargon for customized_context
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            for conv_id, conv_annotations in by_conversation.items():
                try:
                    conversation = await Conversation.find_one(
                        Conversation.conversation_id == conv_id
                    )
                    if not conversation or not conversation.active_transcript:
                        logger.warning(f"ASR finetuning: conversation {conv_id} not found or no transcript")
                        errors += 1
                        continue

                    if not conversation.active_transcript.segments:
                        logger.info(f"ASR finetuning: conversation {conv_id} has no segments, skipping")
                        continue

                    # Reconstruct full WAV audio
                    wav_data = await reconstruct_wav_from_conversation(conv_id)
                    if not wav_data:
                        logger.warning(f"ASR finetuning: no audio for conversation {conv_id}")
                        errors += 1
                        continue

                    # Build training label
                    label = _build_vibevoice_label(conversation)
                    if not label.get("segments"):
                        logger.info(f"ASR finetuning: no segments in label for {conv_id}, skipping")
                        continue

                    # Try to add jargon context from Redis cache
                    if conversation.user_id:
                        jargon = await redis_client.get(f"asr:jargon:{conversation.user_id}")
                        if jargon:
                            label["customized_context"] = [t.strip() for t in jargon.split(",") if t.strip()]

                    # POST to VibeVoice /fine-tune endpoint
                    files = {
                        "audio_files": (f"{conv_id}.wav", io.BytesIO(wav_data), "audio/wav"),
                    }
                    data = {"labels": json.dumps([label])}

                    response = await client.post(
                        f"{vibevoice_url}/fine-tune",
                        files=files,
                        data=data,
                    )

                    if response.status_code == 200:
                        exported += 1
                        logger.info(f"ASR finetuning: exported conversation {conv_id}")
                    else:
                        logger.error(
                            f"ASR finetuning: failed to export {conv_id}: "
                            f"{response.status_code} {response.text[:200]}"
                        )
                        errors += 1
                        continue

                    # Mark annotations as consumed
                    for ann in conv_annotations:
                        ann.processed_by = (
                            f"{ann.processed_by},{_ASR_TRAINING_MARKER}"
                            if ann.processed_by
                            else _ASR_TRAINING_MARKER
                        )
                        ann.updated_at = datetime.now(timezone.utc)
                        await ann.save()
                        consumed += 1

                except Exception as e:
                    logger.error(f"ASR finetuning: error processing conversation {conv_id}: {e}")
                    errors += 1

    finally:
        await redis_client.close()

    logger.info(
        f"ASR finetuning complete: {exported} conversations exported, "
        f"{consumed} annotations consumed, {errors} errors"
    )
    return {
        "conversations_exported": exported,
        "annotations_consumed": consumed,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Job 3: ASR Jargon Extraction
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
