"""
Pydantic response models for TTS services.

These models provide a standardized API response format across all TTS providers.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class SynthesisResult(BaseModel):
    """Result metadata from TTS synthesis (audio returned separately as WAV bytes)."""

    text: str = Field(..., description="The input text that was synthesized")
    duration: float = Field(..., description="Generated audio duration in seconds")
    sample_rate: int = Field(default=24000, description="Audio sample rate in Hz")
    provider: str = Field(..., description="TTS provider name")
    model: str = Field(..., description="Model identifier used")


class VoiceInfo(BaseModel):
    """Information about an available voice / reference speaker."""

    voice_id: str = Field(..., description="Unique voice identifier")
    name: str = Field(..., description="Human-readable voice name")
    description: Optional[str] = Field(default=None, description="Voice description")
    sample_audio_path: Optional[str] = Field(
        default=None, description="Path to sample audio for this voice"
    )


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(..., description="Service status (healthy/initializing)")
    model: str = Field(..., description="Loaded model identifier")
    provider: str = Field(..., description="TTS provider name")


class InfoResponse(BaseModel):
    """Service information response."""

    model_id: str = Field(..., description="Model identifier/name")
    provider: str = Field(..., description="TTS provider name")
    capabilities: List[str] = Field(
        default_factory=list,
        description="List of supported capabilities (e.g., voice_cloning, multilingual)",
    )
    supported_languages: Optional[List[str]] = Field(
        default=None, description="List of supported language codes"
    )
