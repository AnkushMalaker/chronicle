"""
Capability/Requirements system for transcription providers and features.

This module provides explicit contracts between providers and features:
- Providers declare capabilities: what they can produce (diarization, word_timestamps, segments)
- Features declare requirements: what they need to function
- Pipeline validates: check compatibility before running

This enables conditional pipeline execution based on provider capabilities.
For example, VibeVoice provides built-in diarization, so pyannote diarization
can be skipped. Parakeet provides word timestamps, enabling pyannote diarization.
"""

import logging
from enum import Enum
from typing import TYPE_CHECKING, Set, Tuple

if TYPE_CHECKING:
    from advanced_omi_backend.models.conversation import Conversation

logger = logging.getLogger(__name__)


class TranscriptCapability(str, Enum):
    """What a transcription provider can produce."""

    WORD_TIMESTAMPS = "word_timestamps"  # Word-level timing data
    SEGMENTS = "segments"  # Speaker segments in output
    DIARIZATION = "diarization"  # Speaker labels in segments (Speaker 0, Speaker 1, etc.)


class FeatureRequirement(str, Enum):
    """What a feature needs from the transcript."""

    WORDS = "words"  # Needs word-level data
    SEGMENTS = "segments"  # Needs speaker segments
    DIARIZATION = "diarization"  # Needs speaker labels


# Feature requirements registry
# Maps feature names to the set of requirements needed to run that feature
FEATURE_REQUIREMENTS: dict[str, Set[FeatureRequirement]] = {
    # Pyannote diarization needs word timestamps to align speaker segments
    "pyannote_diarization": {FeatureRequirement.WORDS},
    # Summary works with any transcript text
    "conversation_summary": set(),
    # Memory extraction benefits from segments but can work without
    "memory_extraction": set(),
    # Action items works with plain text
    "action_items": set(),
    # Title generation works with plain text
    "title_generation": set(),
}


def check_requirements(
    conversation: "Conversation",
    feature: str,
) -> Tuple[bool, str]:
    """
    Check if a conversation has what a feature needs.

    Args:
        conversation: The conversation to check
        feature: The feature name to validate requirements for

    Returns:
        Tuple of (can_run: bool, reason: str)
        - can_run: True if feature can run, False otherwise
        - reason: "OK" if can run, or description of what's missing
    """
    required = FEATURE_REQUIREMENTS.get(feature, set())

    if not required:
        return True, "OK"

    transcript = conversation.active_transcript
    if not transcript:
        return False, "No active transcript"

    available: Set[FeatureRequirement] = set()

    # Check what's available in the transcript
    if transcript.words:
        available.add(FeatureRequirement.WORDS)
    if transcript.segments:
        available.add(FeatureRequirement.SEGMENTS)

    # Check for diarization source in metadata
    diarization_source = transcript.metadata.get("diarization_source")
    if diarization_source:
        available.add(FeatureRequirement.DIARIZATION)

    missing = required - available
    if missing:
        missing_names = [r.value for r in missing]
        return False, f"Missing: {', '.join(missing_names)}"

    return True, "OK"


def get_provider_capabilities(transcript_version: "Conversation.TranscriptVersion") -> dict:
    """
    Get provider capabilities from transcript version metadata.

    Args:
        transcript_version: The transcript version to check

    Returns:
        Dict of capability name -> bool (whether provider has that capability)
    """
    if not transcript_version or not transcript_version.metadata:
        return {}

    return transcript_version.metadata.get("provider_capabilities", {})


def provider_has_capability(
    transcript_version: "Conversation.TranscriptVersion",
    capability: TranscriptCapability,
) -> bool:
    """
    Check if the provider that created this transcript has a specific capability.

    Args:
        transcript_version: The transcript version to check
        capability: The capability to check for

    Returns:
        True if provider has the capability, False otherwise
    """
    caps = get_provider_capabilities(transcript_version)
    return caps.get(capability.value, False)


def should_run_pyannote_diarization(
    transcript_version: "Conversation.TranscriptVersion",
) -> Tuple[bool, str]:
    """
    Determine if pyannote diarization should run for this transcript.

    Decision logic:
    1. If provider already has diarization -> skip (use provider's diarization)
    2. If provider has word timestamps -> run pyannote
    3. Otherwise -> cannot run (no word timestamps available)

    Args:
        transcript_version: The transcript version to evaluate

    Returns:
        Tuple of (should_run: bool, reason: str)
    """
    if not transcript_version:
        return False, "No transcript version"

    caps = get_provider_capabilities(transcript_version)

    # Check if provider already did diarization
    if caps.get(TranscriptCapability.DIARIZATION.value, False):
        return False, "Provider already diarized"

    # Check if we have word timestamps (required for pyannote)
    if caps.get(TranscriptCapability.WORD_TIMESTAMPS.value, False):
        return True, "Provider has word timestamps, can run pyannote"

    # Check if words exist in transcript (legacy check)
    if transcript_version.words:
        return True, "Words available in transcript"

    return False, "No word timestamps available for diarization"
