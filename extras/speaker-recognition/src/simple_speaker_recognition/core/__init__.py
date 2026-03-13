"""Core speaker recognition components."""

from .audio_backend import AudioBackend
from .models import *
from .unified_speaker_db import UnifiedSpeakerDB

__all__ = ["AudioBackend", "UnifiedSpeakerDB"]
