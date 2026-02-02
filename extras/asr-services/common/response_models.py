"""
Pydantic response models for ASR services.

These models provide a standardized API response format across all providers.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class Word(BaseModel):
    """Word-level transcription with timing information."""

    word: str = Field(..., description="The transcribed word text")
    start: float = Field(..., description="Start time in seconds")
    end: float = Field(..., description="End time in seconds")
    confidence: Optional[float] = Field(
        default=None, description="Confidence score (0.0-1.0)"
    )


class Segment(BaseModel):
    """Segment-level transcription with timing information."""

    text: str = Field(..., description="The transcribed segment text")
    start: float = Field(..., description="Start time in seconds")
    end: float = Field(..., description="End time in seconds")
    speaker: Optional[str] = Field(
        default=None, description="Speaker identifier if available"
    )


class Speaker(BaseModel):
    """Speaker information from diarization."""

    id: str = Field(..., description="Speaker identifier")
    label: Optional[str] = Field(default=None, description="Human-readable speaker label")
    start: float = Field(..., description="Speaker segment start time")
    end: float = Field(..., description="Speaker segment end time")


class TranscriptionResult(BaseModel):
    """Standardized transcription result from any ASR provider."""

    text: str = Field(default="", description="Full transcribed text")
    words: List[Word] = Field(
        default_factory=list, description="Word-level transcriptions with timing"
    )
    segments: List[Segment] = Field(
        default_factory=list, description="Segment-level transcriptions"
    )
    speakers: Optional[List[Speaker]] = Field(
        default=None, description="Speaker diarization information (if available)"
    )
    language: Optional[str] = Field(
        default=None, description="Detected language code"
    )
    duration: Optional[float] = Field(
        default=None, description="Audio duration in seconds"
    )

    def to_dict(self) -> dict:
        """Convert to dictionary, excluding None values."""
        result = {
            "text": self.text,
            "words": [w.model_dump() for w in self.words],
            "segments": [s.model_dump() for s in self.segments],
        }
        if self.speakers is not None:
            result["speakers"] = [s.model_dump() for s in self.speakers]
        if self.language is not None:
            result["language"] = self.language
        if self.duration is not None:
            result["duration"] = self.duration
        return result


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(..., description="Service status (healthy/unhealthy)")
    model: str = Field(..., description="Loaded model identifier")
    provider: str = Field(..., description="ASR provider name")


class InfoResponse(BaseModel):
    """Service information response."""

    model_id: str = Field(..., description="Model identifier/name")
    provider: str = Field(..., description="ASR provider name")
    capabilities: List[str] = Field(
        default_factory=list,
        description="List of supported capabilities (e.g., timestamps, diarization)",
    )
    supported_languages: Optional[List[str]] = Field(
        default=None, description="List of supported language codes"
    )
