"""Pydantic models for speaker recognition API."""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class UserRequest(BaseModel):
    """Request model for user creation."""

    username: str


class UserResponse(BaseModel):
    """Response model for user data."""

    id: int
    username: str
    created_at: str


class EnrollRequest(BaseModel):
    """Request model for speaker enrollment."""

    speaker_id: str
    speaker_name: str
    start: Optional[float] = None
    end: Optional[float] = None


class BatchEnrollRequest(BaseModel):
    """Request model for batch speaker enrollment."""

    speaker_id: str
    speaker_name: str


class IdentifyRequest(BaseModel):
    """Request model for speaker identification."""

    start: Optional[float] = None
    end: Optional[float] = None


class VerifyRequest(BaseModel):
    """Request model for speaker verification."""

    speaker_id: str
    start: Optional[float] = None
    end: Optional[float] = None


class DiarizeRequest(BaseModel):
    """Request model for speaker diarization."""

    min_duration: Optional[float] = Field(
        default=None, description="Minimum duration for speaker segments (seconds)"
    )
    min_speakers: Optional[int] = Field(
        default=None, description="Minimum number of speakers to detect"
    )
    max_speakers: Optional[int] = Field(
        default=None, description="Maximum number of speakers to detect"
    )
    collar: Optional[float] = Field(
        default=2.0,
        description="Collar duration (seconds) around speaker boundaries to merge segments",
    )
    min_duration_off: Optional[float] = Field(
        default=1.5,
        description="Minimum silence duration (seconds) before treating it as a segment boundary",
    )


class DiarizeAndIdentifyRequest(BaseModel):
    """Request model for combined diarization and identification."""

    min_duration: Optional[float] = Field(
        default=0.5, description="Minimum duration for speaker segments (seconds)"
    )
    min_speakers: Optional[int] = Field(
        default=None, description="Minimum number of speakers to detect"
    )
    max_speakers: Optional[int] = Field(
        default=None, description="Maximum number of speakers to detect"
    )
    similarity_threshold: Optional[float] = Field(
        default=None,
        description="Override default similarity threshold for identification",
    )
    identify_only_enrolled: bool = Field(
        default=False, description="Only return segments for enrolled speakers"
    )
    user_id: Optional[int] = Field(
        default=None,
        description="User ID to scope speaker identification to user's enrolled speakers",
    )
    collar: Optional[float] = Field(
        default=2.0,
        description="Collar duration (seconds) around speaker boundaries to merge segments",
    )
    min_duration_off: Optional[float] = Field(
        default=1.5,
        description="Minimum silence duration (seconds) before treating it as a segment boundary",
    )


class SpeakerStatus(str, Enum):
    """Speaker identification status."""

    IDENTIFIED = "identified"
    UNKNOWN = "unknown"
    ERROR = "error"
    PROCESSING = "processing"


class IdentifyResponse(BaseModel):
    """Response model for speaker identification."""

    found: bool = Field(description="Whether a speaker was identified")
    speaker_id: Optional[str] = Field(default=None, description="Identified speaker ID")
    speaker_name: Optional[str] = Field(
        default=None, description="Identified speaker name"
    )
    confidence: float = Field(description="Identification confidence score")
    status: SpeakerStatus = Field(description="Speaker identification status")
    similarity_threshold: float = Field(description="Threshold used for identification")
    duration: float = Field(description="Duration of the processed audio in seconds")


class InferenceRequest(BaseModel):
    """Request model for speaker inference on diarized segments."""

    segments: List[dict] = Field(
        ..., description="Diarized transcript segments with speaker, start, end, text"
    )


class InferenceSegment(BaseModel):
    """Model for enhanced transcript segment with speaker identification."""

    speaker: int = Field(..., description="Original speaker ID from diarization")
    start: float = Field(..., description="Start time in seconds")
    end: float = Field(..., description="End time in seconds")
    text: str = Field(..., description="Transcript text")
    identified_speaker: Optional[str] = Field(
        None, description="Identified speaker name"
    )
    confidence: Optional[float] = Field(
        None, description="Identification confidence score"
    )


# Deepgram API Wrapper Models
class DeepgramTranscriptionRequest(BaseModel):
    """Request model for Deepgram-compatible transcription endpoint."""

    # Core parameters
    model: Optional[str] = Field(
        default="nova-3", description="Model to use for transcription"
    )
    language: Optional[str] = Field(default="multi", description="Language code")
    version: Optional[str] = Field(default="latest", description="Model version")

    # Audio processing
    sample_rate: Optional[int] = Field(default=None, description="Sample rate of audio")
    channels: Optional[int] = Field(
        default=None, description="Number of audio channels"
    )
    encoding: Optional[str] = Field(default=None, description="Audio encoding")

    # Formatting and output
    punctuate: Optional[bool] = Field(default=True, description="Add punctuation")
    profanity_filter: Optional[bool] = Field(
        default=False, description="Filter profanity"
    )
    redact: Optional[List[str]] = Field(
        default=None, description="Information to redact"
    )
    diarize: Optional[bool] = Field(
        default=True, description="Enable speaker diarization"
    )
    diarize_version: Optional[str] = Field(
        default="latest", description="Diarization model version"
    )
    multichannel: Optional[bool] = Field(
        default=False, description="Process multiple channels"
    )
    alternatives: Optional[int] = Field(
        default=1, description="Number of alternative transcripts"
    )
    numerals: Optional[bool] = Field(
        default=True, description="Convert numbers to numerals"
    )
    search: Optional[List[str]] = Field(default=None, description="Search terms")
    replace: Optional[Dict[str, str]] = Field(
        default=None, description="Text replacement rules"
    )
    keywords: Optional[List[str]] = Field(default=None, description="Keywords to boost")
    keyword_boost: Optional[str] = Field(
        default=None, description="Keyword boosting mode"
    )

    # Smart formatting
    smart_format: Optional[bool] = Field(
        default=True, description="Enable smart formatting"
    )
    dates: Optional[bool] = Field(default=True, description="Format dates")
    times: Optional[bool] = Field(default=True, description="Format times")
    currencies: Optional[bool] = Field(default=True, description="Format currencies")
    phone_numbers: Optional[bool] = Field(
        default=True, description="Format phone numbers"
    )
    addresses: Optional[bool] = Field(default=True, description="Format addresses")

    # Structure and segments
    paragraphs: Optional[bool] = Field(
        default=True, description="Organize into paragraphs"
    )
    utterances: Optional[bool] = Field(
        default=True, description="Organize into utterances"
    )
    utt_split: Optional[float] = Field(
        default=None, description="Utterance splitting threshold"
    )
    dictation: Optional[bool] = Field(default=False, description="Dictation mode")
    measurements: Optional[bool] = Field(
        default=True, description="Format measurements"
    )

    # Analysis features
    detect_language: Optional[bool] = Field(
        default=False, description="Detect language automatically"
    )
    detect_topics: Optional[bool] = Field(default=False, description="Detect topics")
    summarize: Optional[Union[bool, str]] = Field(
        default=False, description="Generate summary"
    )
    sentiment: Optional[bool] = Field(default=False, description="Analyze sentiment")
    intents: Optional[bool] = Field(default=False, description="Detect intents")

    # Speaker recognition enhancement (our custom parameters)
    enhance_speakers: Optional[bool] = Field(
        default=True, description="Enable speaker identification enhancement"
    )
    user_id: Optional[int] = Field(
        default=None, description="User ID for speaker identification"
    )
    speaker_confidence_threshold: Optional[float] = Field(
        default=0.15, description="Minimum confidence for speaker identification"
    )


class EnhancedWordInfo(BaseModel):
    """Enhanced word information with speaker identification."""

    word: str
    start: float
    end: float
    confidence: float
    speaker: Optional[int] = None
    speaker_confidence: Optional[float] = None
    punctuated_word: Optional[str] = None

    # Enhanced speaker identification fields
    identified_speaker_id: Optional[str] = None
    identified_speaker_name: Optional[str] = None
    speaker_identification_confidence: Optional[float] = None
    speaker_status: Optional[SpeakerStatus] = None


# Structured Diarization Configuration Models
class DeepgramDiarization(BaseModel):
    """Configuration for Deepgram-based diarization."""

    provider: Literal["deepgram"]
    # No extra options - Deepgram handles internally


class PyannoteDiarization(BaseModel):
    """Configuration for Pyannote-based diarization."""

    provider: Literal["pyannote"]
    min_speakers: Optional[int] = Field(
        default=None, description="Minimum number of speakers to detect"
    )
    max_speakers: Optional[int] = Field(
        default=None, description="Maximum number of speakers to detect"
    )
    collar: float = Field(
        default=2.0, description="Collar duration (seconds) around speaker boundaries"
    )
    min_duration_off: float = Field(
        default=1.5,
        description="Minimum silence duration (seconds) before treating it as a segment boundary",
    )


class DiarizationConfig(BaseModel):
    """Root configuration for diarization processing."""

    diarization: Union[DeepgramDiarization, PyannoteDiarization] = Field(
        ..., description="Diarization provider configuration"
    )


class EnhancedTranscriptionResponse(BaseModel):
    """Enhanced Deepgram-compatible response with speaker identification."""

    metadata: Dict[str, Any]
    results: Dict[str, Any]

    # Additional enhancement metadata
    speaker_enhancement: Optional[Dict[str, Any]] = None
