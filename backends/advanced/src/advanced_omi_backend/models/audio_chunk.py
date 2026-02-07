"""
Audio chunk models for MongoDB-based audio storage.

This module contains the AudioChunkDocument model for storing Opus-compressed
audio chunks in MongoDB. Each chunk represents a 10-second segment of audio
from a conversation.
"""

from datetime import datetime
from typing import Optional

from beanie import Document, Indexed
from bson import Binary
from pydantic import ConfigDict, Field, field_serializer


class AudioChunkDocument(Document):
    """
    MongoDB document representing a 10-second audio chunk.

    Audio chunks are stored in Opus-compressed format for ~94% storage reduction
    compared to raw PCM. Chunks are sequentially numbered and can be reconstructed
    into complete WAV files for playback or batch processing.

    Storage Format:
    - Encoding: Opus (24kbps VBR, optimized for speech)
    - Chunk Duration: 10 seconds (configurable)
    - Original Format: 16kHz, 16-bit, mono PCM
    - Compression Ratio: ~0.047 (94% reduction)

    Indexes:
    - (conversation_id, chunk_index): Primary query pattern for reconstruction
    - conversation_id: Conversation lookup and counting
    - created_at: Maintenance and cleanup operations
    """

    # Pydantic v2 configuration
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Primary identifiers
    conversation_id: Indexed(str) = Field(
        description="Parent conversation ID (UUID format)"
    )
    chunk_index: int = Field(
        description="Sequential chunk number (0-based)",
        ge=0
    )

    # Audio data
    audio_data: bytes = Field(
        description="Opus-encoded audio bytes (stored as BSON Binary in MongoDB)"
    )

    # Size tracking
    original_size: int = Field(
        description="Original PCM size in bytes (before compression)",
        gt=0
    )
    compressed_size: int = Field(
        description="Opus-encoded size in bytes (after compression)",
        gt=0
    )

    # Time boundaries
    start_time: float = Field(
        description="Start time in seconds from conversation start",
        ge=0.0
    )
    end_time: float = Field(
        description="End time in seconds from conversation start",
        gt=0.0
    )
    duration: float = Field(
        description="Chunk duration in seconds (typically 10.0)",
        gt=0.0
    )

    # Audio format
    sample_rate: int = Field(
        default=16000,
        description="Original PCM sample rate (Hz)"
    )
    channels: int = Field(
        default=1,
        description="Number of audio channels (1=mono, 2=stereo)"
    )

    # Optional analysis
    has_speech: Optional[bool] = Field(
        default=None,
        description="Voice Activity Detection result (if available)"
    )

    # Metadata
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Chunk creation timestamp"
    )

    # Soft delete fields
    deleted: bool = Field(
        default=False,
        description="Whether this chunk was soft-deleted"
    )
    deleted_at: Optional[datetime] = Field(
        default=None,
        description="When the chunk was marked as deleted"
    )

    @field_serializer('audio_data')
    def serialize_audio_data(self, v: bytes) -> Binary:
        """
        Convert bytes to BSON Binary for MongoDB storage.

        MongoDB returns BSON Binary as plain bytes during deserialization,
        but expects Binary type for serialization to ensure proper binary data handling.
        """
        if isinstance(v, bytes):
            return Binary(v)
        return v

    class Settings:
        """Beanie document settings."""
        name = "audio_chunks"

        indexes = [
            # Primary query: Retrieve chunks in order for a conversation
            [("conversation_id", 1), ("chunk_index", 1)],

            # Conversation lookup and counting
            "conversation_id",

            # Maintenance queries (cleanup, monitoring)
            "created_at",

            # Soft delete filtering
            "deleted"
        ]

    @property
    def compression_ratio(self) -> float:
        """Calculate compression ratio (compressed/original)."""
        if self.original_size == 0:
            return 0.0
        return self.compressed_size / self.original_size

    @property
    def storage_savings_percent(self) -> float:
        """Calculate storage savings as percentage."""
        return (1 - self.compression_ratio) * 100

    def __repr__(self) -> str:
        """Human-readable representation."""
        return (
            f"AudioChunk(conversation={self.conversation_id[:8]}..., "
            f"index={self.chunk_index}, "
            f"duration={self.duration:.1f}s, "
            f"compression={self.compression_ratio:.3f})"
        )
