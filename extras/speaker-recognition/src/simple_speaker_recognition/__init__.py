"""Simple Speaker Recognition Package.

A comprehensive speaker recognition and diarization system that combines
pyannote diarization with enrollment-based speaker identification.

Features:
- Speaker enrollment from audio samples
- Real-time speaker diarization using pyannote
- WeSpeaker ResNet34 embeddings for high-quality speaker representation
- FAISS-based similarity search for efficient identification
- FastAPI service for easy integration
- React web interface for management

Usage:
    from simple_speaker_recognition.api.service import app
    from simple_speaker_recognition.core.audio_backend import AudioBackend
    from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
"""

__version__ = "0.1.0"
__author__ = "Chronicle Team"

# Import core classes for convenience
from .core.audio_backend import AudioBackend
from .core.unified_speaker_db import UnifiedSpeakerDB

__all__ = ["AudioBackend", "UnifiedSpeakerDB"]
