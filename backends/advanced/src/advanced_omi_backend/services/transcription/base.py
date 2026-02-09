"""
Transcription provider abstract base classes.

This module defines the interfaces for transcription providers.
All concrete provider implementations should inherit from these base classes.

Provider Output Formats:
-----------------------
All providers return a standardized dictionary with the following structure:
{
    "text": str,              # Full transcript text
    "words": List[dict],      # Word-level data (if available)
    "segments": List[dict]    # Speaker segments (if available)
}

Word object format (when available):
{
    "word": str,              # The word text
    "start": float,           # Start time in seconds
    "end": float,             # End time in seconds
    "confidence": float,      # Confidence score (0-1)
    "speaker": int            # Speaker ID (optional)
}

Provider-specific behaviors:
- Deepgram: Returns rich word-level timestamps with confidence scores
- NeMo Parakeet: Returns word-level timestamps (streaming and batch modes)
"""

import abc
from enum import Enum
from typing import Optional


class TranscriptionProvider(Enum):
    """Available transcription providers for audio stream routing."""
    DEEPGRAM = "deepgram"
    PARAKEET = "parakeet"


class BaseTranscriptionProvider(abc.ABC):
    """Abstract base class for all transcription providers."""

    @abc.abstractmethod
    async def transcribe(self, audio_data: bytes, sample_rate: int, **kwargs) -> dict:
        """
        Transcribe audio data to text with word-level timestamps.

        Args:
            audio_data: Raw audio bytes (PCM format)
            sample_rate: Audio sample rate (Hz)
            **kwargs: Additional parameters (e.g. diarize=True for speaker diarization)

        Returns:
            Dictionary containing:
            - text: Transcribed text string
            - words: List of word-level data with timestamps (required)
            - segments: List of speaker segments (empty for non-RTTM providers)
        """
        pass

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Return the provider name for logging."""
        pass

    @property
    @abc.abstractmethod
    def mode(self) -> str:
        """Return 'streaming' or 'batch' for processing mode."""
        pass

    async def connect(self, client_id: Optional[str] = None):
        """Initialize/connect the provider. Default implementation does nothing."""
        pass

    async def disconnect(self):
        """Cleanup/disconnect the provider. Default implementation does nothing."""
        pass


class StreamingTranscriptionProvider(BaseTranscriptionProvider):
    """Base class for streaming transcription providers."""

    @property
    def mode(self) -> str:
        return "streaming"

    @abc.abstractmethod
    async def start_stream(self, client_id: str, sample_rate: int = 16000, diarize: bool = False):
        """Start a transcription stream for a client.

        Args:
            client_id: Unique client identifier
            sample_rate: Audio sample rate
            diarize: Whether to enable speaker diarization (provider-dependent)
        """
        pass

    @abc.abstractmethod
    async def process_audio_chunk(self, client_id: str, audio_chunk: bytes) -> Optional[dict]:
        """
        Process audio chunk and return partial/final transcription.

        Returns:
            None for partial results, dict with transcription for final results
        """
        pass

    @abc.abstractmethod
    async def end_stream(self, client_id: str) -> dict:
        """End stream and return final transcription with word-level timestamps."""
        pass


class BatchTranscriptionProvider(BaseTranscriptionProvider):
    """Base class for batch transcription providers."""

    @property
    def mode(self) -> str:
        return "batch"

    @abc.abstractmethod
    async def transcribe(self, audio_data: bytes, sample_rate: int, diarize: bool = False, context_info: Optional[str] = None, **kwargs) -> dict:
        """Transcribe audio data.

        Args:
            audio_data: Raw audio bytes
            sample_rate: Audio sample rate
            diarize: Whether to enable speaker diarization (provider-dependent)
            context_info: Optional ASR context (hot words, jargon) to boost recognition
            **kwargs: Additional parameters (e.g. Langfuse trace IDs)
        """
        pass
