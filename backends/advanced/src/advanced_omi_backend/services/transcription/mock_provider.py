"""
Mock transcription provider for testing without external API dependencies.

This provider returns predefined transcripts for testing purposes, allowing
tests to run without Deepgram or other external transcription APIs.
"""

from typing import Optional

from .base import BatchTranscriptionProvider


class MockTranscriptionProvider(BatchTranscriptionProvider):
    """
    Mock transcription provider for testing.

    Returns predefined transcripts with word-level timestamps.
    Useful for testing API contracts and data flow without external APIs.
    """

    def __init__(self, fail_mode: bool = False):
        """
        Initialize the mock transcription provider.

        Args:
            fail_mode: If True, transcribe() will raise an exception to simulate transcription failure
        """
        self._is_connected = False
        self.fail_mode = fail_mode

    @property
    def name(self) -> str:
        """Return the provider name for logging."""
        return "mock"

    async def transcribe(
        self,
        audio_data: bytes,
        sample_rate: int,
        diarize: bool = False,
        context_info=None,
        progress_callback=None,
        **kwargs,
    ) -> dict:
        """
        Return a predefined mock transcript or raise exception in fail mode.

        Args:
            audio_data: Raw audio bytes (ignored in mock)
            sample_rate: Audio sample rate (ignored in mock)
            diarize: Whether to enable speaker diarization (ignored in mock)
            context_info: Optional ASR context (ignored in mock)
            progress_callback: Optional callback for batch progress (ignored in mock)
            **kwargs: Additional parameters (ignored in mock)

        Returns:
            Dictionary containing predefined transcript with words and segments

        Raises:
            RuntimeError: If fail_mode is True (simulates transcription failure)
        """
        # Simulate transcription failure if fail_mode is enabled
        if self.fail_mode:
            raise RuntimeError("Mock transcription failure (test mode)")

        # Calculate audio duration from bytes (assuming 16-bit PCM)
        audio_duration = len(audio_data) / (sample_rate * 2)  # 2 bytes per sample

        # Return a mock transcript with word-level timestamps
        # This simulates a real transcription result
        # Note: Made longer to pass test requirements (>100 chars)
        mock_transcript = (
            "This is a mock transcription for testing purposes. "
            "It contains enough words to meet minimum length requirements for automated testing."
        )

        # Generate mock words with timestamps (spread across audio duration)
        words = [
            {"word": "This", "start": 0.0, "end": 0.3, "confidence": 0.99, "speaker": 0},
            {"word": "is", "start": 0.3, "end": 0.5, "confidence": 0.99, "speaker": 0},
            {"word": "a", "start": 0.5, "end": 0.6, "confidence": 0.99, "speaker": 0},
            {"word": "mock", "start": 0.6, "end": 0.9, "confidence": 0.99, "speaker": 0},
            {"word": "transcription", "start": 0.9, "end": 1.5, "confidence": 0.98, "speaker": 0},
            {"word": "for", "start": 1.5, "end": 1.7, "confidence": 0.99, "speaker": 0},
            {"word": "testing", "start": 1.7, "end": 2.1, "confidence": 0.99, "speaker": 0},
            {"word": "purposes", "start": 2.1, "end": 2.6, "confidence": 0.97, "speaker": 0},
            {"word": "It", "start": 2.6, "end": 2.8, "confidence": 0.99, "speaker": 0},
            {"word": "contains", "start": 2.8, "end": 3.2, "confidence": 0.99, "speaker": 0},
            {"word": "enough", "start": 3.2, "end": 3.5, "confidence": 0.99, "speaker": 0},
            {"word": "words", "start": 3.5, "end": 3.8, "confidence": 0.99, "speaker": 0},
            {"word": "to", "start": 3.8, "end": 3.9, "confidence": 0.99, "speaker": 0},
            {"word": "meet", "start": 3.9, "end": 4.1, "confidence": 0.99, "speaker": 0},
            {"word": "minimum", "start": 4.1, "end": 4.5, "confidence": 0.98, "speaker": 0},
            {"word": "length", "start": 4.5, "end": 4.8, "confidence": 0.99, "speaker": 0},
            {"word": "requirements", "start": 4.8, "end": 5.4, "confidence": 0.98, "speaker": 0},
            {"word": "for", "start": 5.4, "end": 5.6, "confidence": 0.99, "speaker": 0},
            {"word": "automated", "start": 5.6, "end": 6.1, "confidence": 0.98, "speaker": 0},
            {"word": "testing", "start": 6.1, "end": 6.5, "confidence": 0.99, "speaker": 0},
        ]

        # Mock segments (single speaker for simplicity)
        segments = [{"speaker": 0, "start": 0.0, "end": 6.5, "text": mock_transcript}]

        return {"text": mock_transcript, "words": words, "segments": segments if diarize else []}

    async def connect(self, client_id: Optional[str] = None):
        """Initialize the mock provider (no-op)."""
        self._is_connected = True

    async def disconnect(self):
        """Cleanup the mock provider (no-op)."""
        self._is_connected = False
