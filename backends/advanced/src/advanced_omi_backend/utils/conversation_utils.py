"""
Conversation utilities - speech detection, title/summary generation.

Extracted from legacy TranscriptionService to be reusable across V2 architecture.
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from advanced_omi_backend.config import get_speech_detection_settings
from advanced_omi_backend.llm_client import async_generate

logger = logging.getLogger(__name__)


def is_meaningful_speech(combined_results: dict) -> bool:
    """
    Convenience wrapper to check if combined transcription results contain meaningful speech.

    This is a shared helper used by both speech detection and conversation timeout logic.

    Args:
        combined_results: Combined results from TranscriptionResultsAggregator with:
            - "text": str - Full transcript text
            - "words": list - Word-level data with confidence and timing
            - "segments": list - Speaker segments
            - "chunk_count": int - Number of chunks processed

    Returns:
        bool: True if meaningful speech detected, False otherwise

    Example:
        >>> combined = await aggregator.get_combined_results(session_id)
        >>> if is_meaningful_speech(combined):
        >>>     print("Meaningful speech detected!")
    """
    if not combined_results.get("text"):
        return False

    transcript_data = {"text": combined_results["text"], "words": combined_results.get("words", [])}

    speech_analysis = analyze_speech(transcript_data)
    return speech_analysis["has_speech"]


def analyze_speech(transcript_data: dict) -> dict:
    """
    Analyze transcript for meaningful speech to determine if conversation should be created.

    Uses configurable thresholds from environment:
    - SPEECH_DETECTION_MIN_WORDS (default: 10)
    - SPEECH_DETECTION_MIN_CONFIDENCE (default: 0.7)
    - SPEECH_DETECTION_MIN_DURATION (default: 10.0)

    Args:
        transcript_data: Dictionary with:
            - "text": str - Full transcript text
            - "words": list - Word-level data with confidence and timing (optional)
                [{"text": str, "confidence": float, "start": float, "end": float}, ...]

    Returns:
        dict: {
            "has_speech": bool,
            "reason": str,
            "word_count": int,
            "duration": float (seconds, 0.0 if no timing data),
            "speech_start": float (optional),
            "speech_end": float (optional),
            "fallback": bool (optional, true if text-only analysis)
        }

    Example:
        >>> result = analyze_speech({"text": "Hello world", "words": [...]})
        >>> if result["has_speech"]:
        >>>     print(f"Speech detected: {result['word_count']} words, {result['duration']}s")
    """
    settings = get_speech_detection_settings()
    words = transcript_data.get("words", [])

    # Method 1: Word-level analysis (preferred - has confidence scores and timing)
    if words:
        # Filter by confidence threshold
        valid_words = [w for w in words if w.get("confidence", 0) >= settings["min_confidence"]]

        if len(valid_words) < settings["min_words"]:
            # Not enough valid words in word-level data - fall through to text-only analysis
            # This handles cases where word-level data is incomplete or low confidence
            logger.debug(f"Only {len(valid_words)} valid words, falling back to text-only analysis")
            # Continue to Method 2 (don't return early)
        else:
            # Calculate speech duration from word timing
            if valid_words:
                speech_start = valid_words[0].get("start", 0)
                speech_end = valid_words[-1].get("end", 0)
                speech_duration = speech_end - speech_start

                # If no timing data (duration = 0), fall back to text-only analysis
                # This happens with some streaming transcription services
                if speech_duration == 0:
                    logger.debug("Word timing data missing, falling back to text-only analysis")
                    # Continue to Method 2 (text-only fallback)
                else:
                    # Check minimum duration threshold when we have timing data
                    min_duration = settings.get("min_duration", 10.0)
                    if speech_duration < min_duration:
                        return {
                            "has_speech": False,
                            "reason": f"Speech too short ({speech_duration:.1f}s < {min_duration}s)",
                            "word_count": len(valid_words),
                            "duration": speech_duration,
                        }

                    return {
                        "has_speech": True,
                        "word_count": len(valid_words),
                        "speech_start": speech_start,
                        "speech_end": speech_end,
                        "duration": speech_duration,
                        "reason": f"Valid speech detected ({len(valid_words)} words, {speech_duration:.1f}s)",
                    }

    # Method 2: Text-only fallback (when no word-level data available)
    text = transcript_data.get("text", "").strip()
    if text:
        word_count = len(text.split())
        if word_count >= settings["min_words"]:
            return {
                "has_speech": True,
                "word_count": word_count,
                "speech_start": 0.0,
                "speech_end": 0.0,
                "duration": 0.0,
                "reason": f"Valid speech detected ({word_count} words, no timing data)",
                "fallback": True,
            }

    # No speech detected
    return {
        "has_speech": False,
        "reason": "No meaningful speech content detected",
        "word_count": 0,
        "duration": 0.0,
    }


async def generate_title(text: str, segments: Optional[list] = None) -> str:
    """
    Generate an LLM-powered title from conversation text.

    Args:
        text: Conversation transcript (used if segments not provided)
        segments: Optional list of speaker segments with structure:
            [{"speaker": str, "text": str, "start": float, "end": float}, ...]
            If provided, uses speaker-aware conversation formatting

    Returns:
        str: Generated title (3-6 words) or fallback

    Note:
        Title intentionally does NOT include speaker names - focuses on topic/theme only.
    """
    # Format conversation text from segments if provided
    if segments:
        conversation_text = ""
        for segment in segments[:10]:  # Use first 10 segments for title generation
            segment_text = segment.get("text", "").strip()
            if segment_text:
                conversation_text += f"{segment_text}\n"
        text = conversation_text if conversation_text.strip() else text

    if not text or len(text.strip()) < 10:
        return "Conversation"

    try:
        prompt = f"""Generate a concise, descriptive title (3-6 words) for this conversation transcript:

"{text[:500]}"

Rules:
- Maximum 6 words
- Capture the main topic or theme
- Do NOT include speaker names or participants
- No quotes or special characters
- Examples: "Planning Weekend Trip", "Work Project Discussion", "Medical Appointment"

Title:"""

        title = await async_generate(prompt, temperature=0.3)
        return title.strip().strip('"').strip("'") or "Conversation"

    except Exception as e:
        logger.warning(f"Failed to generate LLM title: {e}")
        # Fallback to simple title generation
        words = text.split()[:6]
        title = " ".join(words)
        return title[:40] + "..." if len(title) > 40 else title or "Conversation"


async def generate_short_summary(text: str, segments: Optional[list] = None) -> str:
    """
    Generate a brief LLM-powered summary from conversation text.

    Args:
        text: Conversation transcript (used if segments not provided)
        segments: Optional list of speaker segments with structure:
            [{"speaker": str, "text": str, "start": float, "end": float}, ...]
            If provided, includes speaker context in summary

    Returns:
        str: Generated short summary (1-2 sentences, max 120 chars) or fallback
    """
    # Format conversation text from segments if provided
    conversation_text = text
    include_speakers = False

    if segments:
        formatted_text = ""
        speakers_in_conv = set()
        for segment in segments:
            speaker = segment.get("speaker", "")
            segment_text = segment.get("text", "").strip()
            if segment_text:
                if speaker:
                    formatted_text += f"{speaker}: {segment_text}\n"
                    speakers_in_conv.add(speaker)
                else:
                    formatted_text += f"{segment_text}\n"

        if formatted_text.strip():
            conversation_text = formatted_text
            include_speakers = len(speakers_in_conv) > 0

    if not conversation_text or len(conversation_text.strip()) < 10:
        return "No content"

    try:
        speaker_instruction = (
            '- Include speaker names when relevant (e.g., "John discusses X with Sarah")\n'
            if include_speakers
            else ""
        )

        prompt = f"""Generate a brief, informative summary (1-2 sentences, max 120 characters) for this conversation:

"{conversation_text[:1000]}"

Rules:
- Maximum 120 characters
- 1-2 complete sentences
{speaker_instruction}- Capture key topics and outcomes
- Use present tense
- Be specific and informative

Summary:"""

        summary = await async_generate(prompt, temperature=0.3)
        return summary.strip().strip('"').strip("'") or "No content"

    except Exception as e:
        logger.warning(f"Failed to generate LLM short summary: {e}")
        # Fallback to simple summary generation
        return (
            conversation_text[:120] + "..."
            if len(conversation_text) > 120
            else conversation_text or "No content"
        )


# Backward compatibility alias
async def generate_summary(text: str) -> str:
    """
    Backward compatibility wrapper for generate_short_summary.

    Deprecated: Use generate_short_summary instead.
    """
    return await generate_short_summary(text)


async def generate_detailed_summary(text: str, segments: Optional[list] = None) -> str:
    """
    Generate a comprehensive, detailed summary of the conversation.

    This summary provides full information about what was discussed and said,
    correcting transcript errors and removing filler words to create a higher
    quality information set. Not word-for-word like the transcript, but captures
    all key points, context, and meaningful content.

    Args:
        text: Conversation transcript (used if segments not provided)
        segments: Optional list of speaker segments with structure:
            [{"speaker": str, "text": str, "start": float, "end": float}, ...]
            If provided, includes speaker attribution in detailed summary

    Returns:
        str: Comprehensive detailed summary (multiple paragraphs) or fallback
    """
    # Format conversation text from segments if provided
    conversation_text = text
    include_speakers = False

    if segments:
        formatted_text = ""
        speakers_in_conv = set()
        for segment in segments:
            speaker = segment.get("speaker", "")
            segment_text = segment.get("text", "").strip()
            if segment_text:
                if speaker:
                    formatted_text += f"{speaker}: {segment_text}\n"
                    speakers_in_conv.add(speaker)
                else:
                    formatted_text += f"{segment_text}\n"

        if formatted_text.strip():
            conversation_text = formatted_text
            include_speakers = len(speakers_in_conv) > 0

    if not conversation_text or len(conversation_text.strip()) < 10:
        return "No meaningful content to summarize"

    try:
        speaker_instruction = (
            """- Attribute key points and statements to specific speakers when relevant
- Capture the flow of conversation between participants
- Note any agreements, disagreements, or important exchanges
"""
            if include_speakers
            else ""
        )

        prompt = f"""Generate a comprehensive, detailed summary of this conversation transcript.

TRANSCRIPT:
"{conversation_text}"

INSTRUCTIONS:
Your task is to create a high-quality, detailed summary of a conversation transcription that captures the full information and context of what was discussed. This is NOT a brief summary - provide comprehensive coverage.

Rules:
- We know it's a conversation, so no need to say "This conversation involved..."
- Provide complete coverage of all topics, points, and important details discussed
- Correct obvious transcription errors and remove filler words (um, uh, like, you know)
- Organize information logically by topic or chronologically as appropriate
- Use clear, well-structured paragraphs or bullet points, but make the length relative to the amound of content.
- Maintain the meaning and intent of what was said, but improve clarity and coherence
- Include relevant context, decisions made, action items mentioned, and conclusions reached
{speaker_instruction}- Write in a natural, flowing narrative style
- Only include word-for-word quotes if it's more efficiency than rephrasing
- Focus on substantive content - what was actually discussed and decided

Think of this as creating a high-quality information set that someone could use to understand everything important that happened in this conversation without reading the full transcript.

DETAILED SUMMARY:"""

        summary = await async_generate(prompt, temperature=0.3)
        return summary.strip().strip('"').strip("'") or "No meaningful content to summarize"

    except Exception as e:
        logger.warning(f"Failed to generate detailed summary: {e}")
        # Fallback to returning cleaned transcript
        lines = conversation_text.split("\n")
        cleaned = "\n".join(line.strip() for line in lines if line.strip())
        return (
            cleaned[:2000] + "..."
            if len(cleaned) > 2000
            else cleaned or "No meaningful content to summarize"
        )


# Backward compatibility aliases for deprecated speaker-specific methods
async def generate_title_with_speakers(segments: list) -> str:
    """
    Deprecated: Use generate_title(text, segments=segments) instead.

    Backward compatibility wrapper.
    """
    if not segments:
        return "Conversation"
    # Extract text from segments for compatibility
    text = "\n".join(s.get("text", "") for s in segments if s.get("text"))
    return await generate_title(text, segments=segments)


async def generate_summary_with_speakers(segments: list) -> str:
    """
    Deprecated: Use generate_short_summary(text, segments=segments) instead.

    Backward compatibility wrapper.
    """
    if not segments:
        return "No content"
    # Extract text from segments for compatibility
    text = "\n".join(s.get("text", "") for s in segments if s.get("text"))
    return await generate_short_summary(text, segments=segments)


# ============================================================================
# Conversation Job Helpers
# ============================================================================



def extract_speakers_from_segments(segments: List[Dict[str, Any]]) -> List[str]:
    """
    Extract unique speaker names from segments.

    Args:
        segments: List of segments with speaker information

    Returns:
        List of unique speaker names (excluding "Unknown")
    """
    speakers = []
    if segments:
        for seg in segments:
            speaker = seg.get("speaker", "Unknown")
            if speaker and speaker != "Unknown" and speaker not in speakers:
                speakers.append(speaker)
    return speakers


async def track_speech_activity(
    speech_analysis: Dict[str, Any], last_word_count: int, conversation_id: str, redis_client
) -> tuple[float, int]:
    """
    Track new speech activity and update last speech timestamp.

    Uses word count instead of chunk count to avoid false positives from noise/silence.

    Args:
        speech_analysis: Speech analysis results from analyze_speech()
        last_word_count: Previous word count
        conversation_id: Conversation ID for Redis key
        redis_client: Redis client instance

    Returns:
        Tuple of (last_meaningful_speech_time, new_word_count)
    """
    current_word_count = speech_analysis.get("word_count", 0)

    if current_word_count > last_word_count:
        last_meaningful_speech_time = time.time()

        # Store timestamp in Redis for visibility/debugging
        await redis_client.set(
            f"conversation:last_speech:{conversation_id}",
            last_meaningful_speech_time,
            ex=86400,  # 24 hour TTL
        )
        logger.debug(
            f"🗣️ New speech detected (word count: {current_word_count}), updated last_speech timestamp"
        )

        return last_meaningful_speech_time, current_word_count

    # No new speech - return None to indicate no update
    return None, last_word_count


async def update_job_progress_metadata(
    current_job,
    conversation_id: str,
    session_id: str,
    client_id: str,
    combined: Dict[str, Any],
    speech_analysis: Dict[str, Any],
    speakers: List[str],
    last_meaningful_speech_time: float,
) -> None:
    """
    Update job metadata with current conversation progress.

    Args:
        current_job: Current RQ job instance
        conversation_id: Conversation ID
        session_id: Session ID
        client_id: Client ID
        combined: Combined transcription results
        speech_analysis: Speech analysis results
        speakers: List of speakers
        last_meaningful_speech_time: Timestamp of last speech
    """
    if not current_job:
        return

    if not current_job.meta:
        current_job.meta = {}

    # Set created_at only once (first time we update metadata)
    if "created_at" not in current_job.meta:
        current_job.meta["created_at"] = datetime.now().isoformat()

    current_job.meta.update(
        {
            "conversation_id": conversation_id,
            "client_id": client_id,  # Ensure client_id is always present
            "transcript": (
                combined["text"][:500] + "..." if len(combined["text"]) > 500 else combined["text"]
            ),  # First 500 chars
            "transcript_length": len(combined["text"]),
            "speakers": speakers,
            "word_count": speech_analysis.get("word_count", 0),
            "duration_seconds": speech_analysis.get("duration", 0),
            "has_speech": speech_analysis.get("has_speech", False),
            "last_update": datetime.now().isoformat(),
            "inactivity_seconds": time.time() - last_meaningful_speech_time,
            "chunks_processed": combined["chunk_count"],
        }
    )
    current_job.save_meta()


async def mark_conversation_deleted(conversation_id: str, deletion_reason: str) -> None:
    """
    Mark a conversation as deleted with a specific reason.

    Uses soft delete pattern - conversation remains in database but marked as deleted.

    Args:
        conversation_id: Conversation ID to mark as deleted
        deletion_reason: Reason for deletion (e.g., "no_meaningful_speech", "audio_file_not_ready")
    """
    from advanced_omi_backend.models.conversation import Conversation

    logger.warning(
        f"🗑️ Marking conversation {conversation_id} as deleted - reason: {deletion_reason}"
    )

    conversation = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if conversation:
        conversation.deleted = True
        conversation.deletion_reason = deletion_reason
        conversation.deleted_at = datetime.utcnow()
        await conversation.save()
        logger.info(f"✅ Marked conversation {conversation_id} as deleted")
