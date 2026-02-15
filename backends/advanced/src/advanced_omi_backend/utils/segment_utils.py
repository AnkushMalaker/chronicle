"""
Shared utilities for segment classification and non-speech detection.

Used by transcription pipeline, speaker recognition, and annotation system.
"""

import re

# Matches text that is entirely a bracketed tag like [laughter], [Music], [Environmental Sounds]
NON_SPEECH_PATTERN = re.compile(r"^\[.*\]$")


def classify_segment_text(text: str) -> str:
    """
    Classify segment text as speech, event, or mixed.

    Returns:
        "event" — entire text is a non-speech tag like [laughter]
        "mixed" — text contains both speech and non-speech tokens
        "speech" — normal speech text
    """
    text = text.strip()
    if not text:
        return "event"

    if NON_SPEECH_PATTERN.match(text):
        return "event"

    # Check if text contains any bracketed tokens mixed with speech
    if re.search(r"\[.*?\]", text):
        # Has bracketed tokens but also has other content
        stripped = re.sub(r"\[.*?\]", "", text).strip()
        if stripped:
            return "mixed"
        return "event"

    return "speech"


def strip_non_speech_tokens(text: str) -> str:
    """Remove bracketed non-speech tokens from text, returning speech-only content."""
    return re.sub(r"\[.*?\]", "", text).strip()


def is_non_speech(text: str, speaker: str = "") -> bool:
    """
    Check if a segment represents non-speech content.

    A segment is non-speech if:
    - Text is empty
    - Text is entirely a bracketed tag like [Music]
    - Speaker label is None/empty
    """
    text = text.strip()
    if not text:
        return True
    if NON_SPEECH_PATTERN.match(text):
        return True
    if speaker in ("None", "none", ""):
        return True
    return False
