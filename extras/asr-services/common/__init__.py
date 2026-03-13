"""
Common utilities for ASR services.

This module provides shared components used across all ASR providers:
- BaseASRService: Abstract base class for ASR service implementations
- Audio utilities: Resampling, format conversion, chunking
- Response models: Pydantic models for standardized API responses
"""

from common.audio_utils import (
    convert_audio_to_numpy,
    load_audio_file,
    resample_audio,
    save_audio_file,
)
from common.base_service import BaseASRService, create_asr_app
from common.response_models import (
    HealthResponse,
    InfoResponse,
    Segment,
    Speaker,
    TranscriptionResult,
    Word,
)

__all__ = [
    # Response models
    "TranscriptionResult",
    "Word",
    "Segment",
    "Speaker",
    "HealthResponse",
    "InfoResponse",
    # Audio utilities
    "convert_audio_to_numpy",
    "resample_audio",
    "load_audio_file",
    "save_audio_file",
    # Base service
    "BaseASRService",
    "create_asr_app",
]
