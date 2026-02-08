"""
Waveform visualization data model for conversations.

This module provides the WaveformData model for storing pre-computed
waveform visualization data, enabling UI to display audio waveforms
without real-time decoding.
"""

from datetime import datetime
from typing import List, Optional

from beanie import Document, Indexed
from pydantic import Field


class WaveformData(Document):
    """Pre-computed waveform visualization for conversations."""

    # Link to parent conversation
    conversation_id: Indexed(str) = Field(
        description="Parent conversation ID (unique per conversation)"
    )

    # Waveform amplitude data
    samples: List[float] = Field(
        description="Amplitude samples normalized to [-1.0, 1.0] range"
    )
    sample_rate: int = Field(
        description="Samples per second (e.g., 10 = 1 sample per 100ms)"
    )

    # Metadata
    duration_seconds: float = Field(description="Total audio duration in seconds")
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When this waveform was generated"
    )
    processing_time_seconds: Optional[float] = Field(
        None,
        description="Time taken to generate waveform"
    )

    class Settings:
        name = "waveforms"
        indexes = [
            "conversation_id",  # Unique lookup by conversation
        ]
