"""
Common utilities for ASR services.

This module provides shared components used across all ASR providers:
- BaseASRService: Abstract base class for ASR service implementations
- Audio utilities: Resampling, format conversion, chunking
- Response models: Pydantic models for standardized API responses
"""

from common.response_models import (
    TranscriptionResult,
    Word,
    Segment,
    Speaker,
    HealthResponse,
    InfoResponse,
)
from common.audio_utils import (
    convert_audio_to_numpy,
    resample_audio,
    load_audio_file,
    save_audio_file,
)
from common.base_service import BaseASRService, create_asr_app

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
