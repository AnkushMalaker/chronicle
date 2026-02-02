"""
NeMo ASR Provider.

NVIDIA NeMo ASR backend for Parakeet, Canary, and other NeMo models.
Includes enhanced chunking support for long audio processing.
"""

from providers.nemo.service import NemoService
from providers.nemo.transcriber import NemoTranscriber

__all__ = ["NemoService", "NemoTranscriber"]
