"""Voice-activity-detection provider interface.

Providers score 16-bit mono PCM and return per-frame speech probabilities.
Implementations may keep streaming state between ``score()`` calls, so use one
provider instance per audio stream (e.g., per conversation) and feed it chunks
in order. Obtain instances via :func:`advanced_omi_backend.services.vad.get_vad_provider`.
"""

from abc import ABC, abstractmethod

import numpy as np


class VADProvider(ABC):
    """Scores PCM audio with per-frame speech probabilities in [0, 1]."""

    name: str
    frame_hop_ms: float

    @abstractmethod
    def score(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        """Score 16-bit mono PCM (int16 ndarray) at ``sample_rate``.

        Returns a float ndarray of per-frame speech probabilities, one frame
        every ``frame_hop_ms`` milliseconds. May carry streaming state from
        previous calls on the same instance.
        """
