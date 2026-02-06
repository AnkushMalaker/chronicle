"""Mock speaker recognition client for testing without heavy ML dependencies."""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class MockSpeakerRecognitionClient:
    """
    Mock speaker recognition client that returns pre-computed segments.

    Used in test environments to avoid running resource-intensive speaker
    recognition service. Segments are based on test_data.py expectations.
    """

    # Map audio filenames to mock segment data
    # Segments follow the structure expected by the backend:
    # {
    #   "start": float,          # Start time in seconds
    #   "end": float,            # End time in seconds
    #   "text": str,             # Transcript text for this segment
    #   "speaker": int,          # Speaker label (0, 1, 2, etc.)
    #   "identified_as": str,    # Speaker name or "Unknown"
    #   "confidence": float      # Optional confidence score
    # }

    MOCK_SEGMENTS = {
        "DIY_Experts_Glass_Blowing_16khz_mono_1min.wav": [
            {
                "start": 0.0,
                "end": 10.08,
                "speaker": 0,
                "identified_as": "Unknown",
                "text": "The pumpkin that'll last for forever. Finally. Does it count? Today, we're taking a glass blowing class.",
                "confidence": 0.95
            },
            {
                "start": 10.28,
                "end": 20.255,
                "speaker": 0,
                "identified_as": "Unknown",
                "text": "I'm sweating already. We've worked with a lot of materials before, but we've only scratched the surface",
                "confidence": 0.93
            },
            {
                "start": 20.455,
                "end": 21.895,
                "speaker": 1,
                "identified_as": "Unknown",
                "text": "when it comes to glass",
                "confidence": 0.91
            },
            {
                "start": 22.095,
                "end": 23.615,
                "speaker": 0,
                "identified_as": "Unknown",
                "text": "and that's because",
                "confidence": 0.94
            },
            {
                "start": 23.815,
                "end": 28.135,
                "speaker": 1,
                "identified_as": "Unknown",
                "text": "a little intimidating. We've got about 400 pounds",
                "confidence": 0.92
            },
            {
                "start": 28.335,
                "end": 43.08,
                "speaker": 0,
                "identified_as": "Unknown",
                "text": "of liquid glass in this furnace right here. Nick's gonna really help us out. Nick, I'm excited and nervous. Me too.",
                "confidence": 0.96
            },
            {
                "start": 43.28,
                "end": 44.48,
                "speaker": 1,
                "identified_as": "Unknown",
                "text": "So we're gonna",
                "confidence": 0.90
            },
            {
                "start": 44.68,
                "end": 46.76,
                "speaker": 0,
                "identified_as": "Unknown",
                "text": "make what's called a trumpet",
                "confidence": 0.95
            },
            {
                "start": 46.96,
                "end": 50.24,
                "speaker": 0,
                "identified_as": "Unknown",
                "text": "flower. We're using gravity as a tool.",
                "confidence": 0.93
            }
        ]
    }

    def __init__(self):
        """Initialize mock client."""
        logger.info("🎤 Mock speaker recognition client initialized")

    async def diarize_identify_match(
        self,
        conversation_id: str,
        backend_token: str,
        transcript_data: Dict,
        user_id: Optional[str] = None
    ) -> Dict:
        """
        Return pre-computed mock segments for known test audio files.

        Args:
            conversation_id: Not used in mock (audio filename derived from transcript)
            backend_token: Not used in mock
            transcript_data: Dict with 'text' and 'words' - used to identify audio file
            user_id: Not used in mock

        Returns:
            Dictionary with 'segments' array matching speaker service format
        """
        logger.info(f"🎤 Mock speaker client processing conversation: {conversation_id[:12]}...")

        # Try to identify which test audio this is from the transcript
        transcript_text = transcript_data.get("text", "").lower()

        # Match by transcript content
        if "glass blowing" in transcript_text or "glass" in transcript_text:
            filename = "DIY_Experts_Glass_Blowing_16khz_mono_1min.wav"
            if filename in self.MOCK_SEGMENTS:
                segments = self.MOCK_SEGMENTS[filename]
                logger.info(f"🎤 Mock returning {len(segments)} segments for DIY Glass Blowing audio")
                return {"segments": segments}

        # Fallback: Create single generic segment
        logger.warning(f"🎤 Mock: No pre-computed segments found, creating generic segment")

        # Get duration from words if available
        words = transcript_data.get("words", [])
        if words:
            duration = words[-1].get("end", 60.0)
        else:
            duration = 60.0

        return {
            "segments": [{
                "start": 0.0,
                "end": duration,
                "speaker": 0,
                "identified_as": "Unknown",
                "text": transcript_data.get("text", ""),
                "confidence": 0.85
            }]
        }

    async def identify_segment(
        self,
        audio_wav_bytes: bytes,
        user_id: Optional[str] = None,
        similarity_threshold: Optional[float] = None,
    ) -> Dict:
        """Mock identify_segment - returns no identification."""
        logger.info("🎤 Mock identify_segment called")
        return {
            "found": False,
            "speaker_id": None,
            "speaker_name": None,
            "confidence": 0.0,
            "status": "unknown",
            "duration": 0.0,
        }

    async def identify_provider_segments(
        self,
        conversation_id: str,
        segments: List[Dict],
        user_id: Optional[str] = None,
    ) -> Dict:
        """Mock identify_provider_segments - returns segments with original labels."""
        logger.info(f"🎤 Mock identify_provider_segments: {len(segments)} segments")
        return {
            "segments": [
                {
                    "start": seg.get("start", 0.0),
                    "end": seg.get("end", 0.0),
                    "text": seg.get("text", ""),
                    "speaker": seg.get("speaker", "Unknown"),
                    "identified_as": seg.get("speaker", "Unknown"),
                    "confidence": 0.0,
                    "status": "unknown",
                }
                for seg in segments
            ]
        }
