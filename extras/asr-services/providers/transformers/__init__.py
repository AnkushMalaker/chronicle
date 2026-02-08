"""
Transformers ASR Provider.

HuggingFace Transformers backend for general ASR models.
Supports models like VibeVoice-ASR, Whisper variants, and custom fine-tuned models.
"""

from providers.transformers.service import TransformersService
from providers.transformers.transcriber import TransformersTranscriber

__all__ = ["TransformersService", "TransformersTranscriber"]
