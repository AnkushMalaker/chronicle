"""
Conversation models for Chronicle backend.

This module contains Beanie Document and Pydantic models for conversations,
transcript versions, and memory versions.
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from beanie import Document, Indexed
from pydantic import BaseModel, Field, computed_field, field_validator, model_validator
from pymongo import IndexModel


class Conversation(Document):
    """Complete conversation model with versioned processing."""

    # Nested Enums - Note: TranscriptProvider accepts any string value for flexibility

    class MemoryProvider(str, Enum):
        """Supported memory providers."""
        CHRONICLE = "chronicle"
        OPENMEMORY_MCP = "openmemory_mcp"
        MYCELIA = "mycelia"
        FRIEND_LITE = "friend_lite"  # Legacy value

    class ConversationStatus(str, Enum):
        """Conversation processing status."""
        ACTIVE = "active"  # Has running jobs or open websocket
        COMPLETED = "completed"  # All jobs succeeded
        FAILED = "failed"  # One or more jobs failed

    class EndReason(str, Enum):
        """Reason for conversation ending."""
        USER_STOPPED = "user_stopped"  # User manually stopped recording
        INACTIVITY_TIMEOUT = "inactivity_timeout"  # No speech detected for threshold period
        WEBSOCKET_DISCONNECT = "websocket_disconnect"  # Connection lost (Bluetooth, network, etc.)
        MAX_DURATION = "max_duration"  # Hit maximum conversation duration
        CLOSE_REQUESTED = "close_requested"  # External close signal (API, plugin, button)
        ERROR = "error"  # Processing error forced conversation end
        UNKNOWN = "unknown"  # Unknown or legacy reason

    # Nested Models
    class Word(BaseModel):
        """Individual word with timestamp in a transcript."""
        word: str = Field(description="Word text")
        start: float = Field(description="Start time in seconds")
        end: float = Field(description="End time in seconds")
        confidence: Optional[float] = Field(None, description="Confidence score (0-1)")

    class SpeakerSegment(BaseModel):
        """Individual speaker segment in a transcript."""
        start: float = Field(description="Start time in seconds")
        end: float = Field(description="End time in seconds")
        text: str = Field(description="Transcript text for this segment")
        speaker: str = Field(description="Speaker identifier")
        identified_as: Optional[str] = Field(None, description="Speaker name from speaker recognition (None if not identified)")
        confidence: Optional[float] = Field(None, description="Confidence score (0-1)")
        words: List["Conversation.Word"] = Field(default_factory=list, description="Word-level timestamps for this segment")

    class TranscriptVersion(BaseModel):
        """Version of a transcript with processing metadata."""
        version_id: str = Field(description="Unique version identifier")
        transcript: Optional[str] = Field(None, description="Full transcript text")
        words: List["Conversation.Word"] = Field(
            default_factory=list,
            description="Word-level timestamps for entire transcript"
        )
        segments: List["Conversation.SpeakerSegment"] = Field(
            default_factory=list,
            description="Speaker segments (filled by speaker recognition)"
        )
        provider: Optional[str] = Field(None, description="Transcription provider used (deepgram, parakeet, vibevoice, etc.)")
        model: Optional[str] = Field(None, description="Model used (e.g., nova-3, parakeet)")
        created_at: datetime = Field(description="When this version was created")
        processing_time_seconds: Optional[float] = Field(None, description="Time taken to process")
        diarization_source: Optional[str] = Field(
            None,
            description="Source of speaker diarization: 'provider' (transcription service), 'pyannote' (speaker recognition), or None"
        )
        metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional provider-specific metadata")

    class MemoryVersion(BaseModel):
        """Version of memory extraction with processing metadata."""
        version_id: str = Field(description="Unique version identifier")
        memory_count: int = Field(description="Number of memories extracted")
        transcript_version_id: str = Field(description="Which transcript version was used")
        provider: "Conversation.MemoryProvider" = Field(description="Memory provider used")
        model: Optional[str] = Field(None, description="Model used (e.g., gpt-4o-mini, llama3)")
        created_at: datetime = Field(description="When this version was created")
        processing_time_seconds: Optional[float] = Field(None, description="Time taken to process")
        metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional provider-specific metadata")

    # Core identifiers
    conversation_id: Indexed(str, unique=True) = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique conversation identifier")
    user_id: Indexed(str) = Field(description="User who owns this conversation")
    client_id: Indexed(str) = Field(description="Client device identifier")

    # External file tracking (for deduplication of imported files)
    external_source_id: Optional[str] = Field(
        None,
        description="External file identifier (e.g., Google Drive file_id) for deduplication"
    )
    external_source_type: Optional[str] = Field(
        None,
        description="Type of external source (gdrive, dropbox, s3, etc.)"
    )

    # MongoDB chunk-based audio storage (new system)
    audio_chunks_count: Optional[int] = Field(
        None,
        description="Total number of 10-second audio chunks stored in MongoDB"
    )
    audio_total_duration: Optional[float] = Field(
        None,
        description="Total audio duration in seconds (sum of all chunks)"
    )
    audio_compression_ratio: Optional[float] = Field(
        None,
        description="Compression ratio (compressed_size / original_size), typically ~0.047 for Opus"
    )

    # Markers (e.g., button events) captured during the session
    markers: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Markers captured during audio session (button events, bookmarks, etc.)"
    )

    # Creation metadata
    created_at: Indexed(datetime) = Field(default_factory=datetime.utcnow, description="When the conversation was created")

    # Processing status tracking
    deleted: bool = Field(False, description="Whether this conversation was deleted due to processing failure")
    deletion_reason: Optional[str] = Field(None, description="Reason for deletion (no_meaningful_speech, audio_file_not_ready, etc.)")
    deleted_at: Optional[datetime] = Field(None, description="When the conversation was marked as deleted")

    # Always persist audio flag and processing status
    processing_status: Optional[str] = Field(
        None,
        description="Processing status: pending_transcription, transcription_failed, completed"
    )
    always_persist: bool = Field(
        default=False,
        description="Flag indicating conversation was created for audio persistence"
    )

    # Conversation completion tracking
    end_reason: Optional["Conversation.EndReason"] = Field(None, description="Reason why the conversation ended")
    completed_at: Optional[datetime] = Field(None, description="When the conversation was completed/closed")

    # Summary fields (auto-generated from transcript)
    title: Optional[str] = Field(None, description="Auto-generated conversation title")
    summary: Optional[str] = Field(None, description="Auto-generated short summary (1-2 sentences)")
    detailed_summary: Optional[str] = Field(None, description="Auto-generated detailed summary (comprehensive, corrected content)")

    # Versioned processing
    transcript_versions: List["Conversation.TranscriptVersion"] = Field(
        default_factory=list,
        description="All transcript processing attempts"
    )
    memory_versions: List["Conversation.MemoryVersion"] = Field(
        default_factory=list,
        description="All memory extraction attempts"
    )

    # Active version pointers
    active_transcript_version: Optional[str] = Field(
        None,
        description="Version ID of currently active transcript"
    )
    active_memory_version: Optional[str] = Field(
        None,
        description="Version ID of currently active memory extraction"
    )

    # Legacy fields removed - use transcript_versions[active_transcript_version] and memory_versions[active_memory_version]
    # Frontend should access: conversation.active_transcript.segments, conversation.active_transcript.transcript

    @model_validator(mode='before')
    @classmethod
    def clean_legacy_data(cls, data: Any) -> Any:
        """Clean up legacy/malformed data before Pydantic validation."""

        if not isinstance(data, dict):
            return data

        # Fix malformed transcript_versions (from old schema versions)
        if 'transcript_versions' in data and isinstance(data['transcript_versions'], list):
            for version in data['transcript_versions']:
                if isinstance(version, dict):
                    # If segments is not a list, clear it
                    if 'segments' in version and not isinstance(version['segments'], list):
                        version['segments'] = []
                    # If transcript is a dict, clear it
                    if 'transcript' in version and isinstance(version['transcript'], dict):
                        version['transcript'] = None
                    # Normalize provider to lowercase (legacy data had "Deepgram" instead of "deepgram")
                    if 'provider' in version and isinstance(version['provider'], str):
                        version['provider'] = version['provider'].lower()
                    # Fix speaker IDs in segments (legacy data had integers, need strings)
                    if 'segments' in version and isinstance(version['segments'], list):
                        for segment in version['segments']:
                            if isinstance(segment, dict) and 'speaker' in segment:
                                if isinstance(segment['speaker'], int):
                                    segment['speaker'] = f"Speaker {segment['speaker']}"
                                elif not isinstance(segment['speaker'], str):
                                    segment['speaker'] = "unknown"

        return data

    @computed_field
    @property
    def active_transcript(self) -> Optional["Conversation.TranscriptVersion"]:
        """Get the currently active transcript version."""
        if not self.active_transcript_version:
            return None

        for version in self.transcript_versions:
            if version.version_id == self.active_transcript_version:
                return version
        return None

    @computed_field
    @property
    def active_memory(self) -> Optional["Conversation.MemoryVersion"]:
        """Get the currently active memory version."""
        if not self.active_memory_version:
            return None

        for version in self.memory_versions:
            if version.version_id == self.active_memory_version:
                return version
        return None

    # Convenience properties that return data from active transcript version
    @computed_field
    @property
    def transcript(self) -> Optional[str]:
        """Get transcript text from active transcript version."""
        return self.active_transcript.transcript if self.active_transcript else None

    @computed_field
    @property
    def segments(self) -> List["Conversation.SpeakerSegment"]:
        """Get segments from active transcript version."""
        return self.active_transcript.segments if self.active_transcript else []

    @computed_field
    @property
    def segment_count(self) -> int:
        """Get segment count from active transcript version."""
        return len(self.segments) if self.segments else 0

    @computed_field
    @property
    def memory_count(self) -> int:
        """Get memory count from active memory version."""
        return self.active_memory.memory_count if self.active_memory else 0

    @computed_field
    @property
    def has_memory(self) -> bool:
        """Check if conversation has any memory versions."""
        return len(self.memory_versions) > 0

    @computed_field
    @property
    def transcript_version_count(self) -> int:
        """Get count of transcript versions."""
        return len(self.transcript_versions)

    @computed_field
    @property
    def memory_version_count(self) -> int:
        """Get count of memory versions."""
        return len(self.memory_versions)

    @computed_field
    @property
    def active_transcript_version_number(self) -> Optional[int]:
        """Get 1-based version number of the active transcript version."""
        if not self.active_transcript_version:
            return None
        for i, version in enumerate(self.transcript_versions):
            if version.version_id == self.active_transcript_version:
                return i + 1
        return None

    @computed_field
    @property
    def active_memory_version_number(self) -> Optional[int]:
        """Get 1-based version number of the active memory version."""
        if not self.active_memory_version:
            return None
        for i, version in enumerate(self.memory_versions):
            if version.version_id == self.active_memory_version:
                return i + 1
        return None

    def add_transcript_version(
        self,
        version_id: str,
        transcript: str,
        words: Optional[List["Conversation.Word"]] = None,
        segments: Optional[List["Conversation.SpeakerSegment"]] = None,
        provider: str = None,  # Provider name from config.yml (deepgram, parakeet, etc.)
        model: Optional[str] = None,
        processing_time_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        set_as_active: bool = True
    ) -> "Conversation.TranscriptVersion":
        """Add a new transcript version and optionally set it as active."""
        new_version = Conversation.TranscriptVersion(
            version_id=version_id,
            transcript=transcript,
            words=words or [],
            segments=segments or [],
            provider=provider,
            model=model,
            created_at=datetime.now(),
            processing_time_seconds=processing_time_seconds,
            metadata=metadata or {}
        )

        self.transcript_versions.append(new_version)

        if set_as_active:
            self.active_transcript_version = version_id

        return new_version

    def add_memory_version(
        self,
        version_id: str,
        memory_count: int,
        transcript_version_id: str,
        provider: "Conversation.MemoryProvider",
        model: Optional[str] = None,
        processing_time_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        set_as_active: bool = True
    ) -> "Conversation.MemoryVersion":
        """Add a new memory version and optionally set it as active."""
        new_version = Conversation.MemoryVersion(
            version_id=version_id,
            memory_count=memory_count,
            transcript_version_id=transcript_version_id,
            provider=provider,
            model=model,
            created_at=datetime.now(),
            processing_time_seconds=processing_time_seconds,
            metadata=metadata or {}
        )

        self.memory_versions.append(new_version)

        if set_as_active:
            self.active_memory_version = version_id

        return new_version

    def set_active_transcript_version(self, version_id: str) -> bool:
        """Set a specific transcript version as active."""
        for version in self.transcript_versions:
            if version.version_id == version_id:
                self.active_transcript_version = version_id
                return True
        return False

    def set_active_memory_version(self, version_id: str) -> bool:
        """Set a specific memory version as active."""
        for version in self.memory_versions:
            if version.version_id == version_id:
                self.active_memory_version = version_id
                return True
        return False

    class Settings:
        name = "conversations"
        indexes = [
            "conversation_id",
            "user_id",
            "created_at",
            [("user_id", 1), ("deleted", 1), ("created_at", -1)],  # Compound index for paginated list queries
            IndexModel([("external_source_id", 1)], sparse=True)  # Sparse index for deduplication
        ]


# Factory function for creating conversations
def create_conversation(
    user_id: str,
    client_id: str,
    conversation_id: Optional[str] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    transcript: Optional[str] = None,
    segments: Optional[List["Conversation.SpeakerSegment"]] = None,
    external_source_id: Optional[str] = None,
    external_source_type: Optional[str] = None,
) -> Conversation:
    """
    Factory function to create a new conversation.

    Args:
        user_id: User who owns this conversation
        client_id: Client device identifier
        conversation_id: Optional unique conversation identifier (auto-generated if not provided)
        title: Optional conversation title
        summary: Optional conversation summary
        transcript: Optional transcript text
        segments: Optional speaker segments
        external_source_id: Optional external file ID for deduplication (e.g., Google Drive file_id)
        external_source_type: Optional external source type (gdrive, dropbox, etc.)

    Returns:
        Conversation instance
    """
    # Build the conversation data
    conv_data = {
        "user_id": user_id,
        "client_id": client_id,
        "created_at": datetime.now(),
        "title": title,
        "summary": summary,
        "transcript_versions": [],
        "active_transcript_version": None,
        "memory_versions": [],
        "active_memory_version": None,
        "external_source_id": external_source_id,
        "external_source_type": external_source_type,
    }

    # Only set conversation_id if provided, otherwise let the model auto-generate it
    if conversation_id is not None:
        conv_data["conversation_id"] = conversation_id

    return Conversation(**conv_data)