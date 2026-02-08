"""
Faster-Whisper ASR Provider.

Fast Whisper inference using CTranslate2 backend.
Supports any Whisper-based model converted to CTranslate2 format.
"""

from providers.faster_whisper.service import FasterWhisperService
from providers.faster_whisper.transcriber import FasterWhisperTranscriber

__all__ = ["FasterWhisperService", "FasterWhisperTranscriber"]
