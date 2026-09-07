"""TEN VAD provider (https://github.com/ten-framework/ten-vad).

Lightweight C/ONNX model, numpy-only Python binding. Requires 16 kHz mono
int16 PCM; scores one frame per 256-sample hop (16 ms).
"""

import numpy as np
from ten_vad import TenVad

from backend.services.vad.base import VADProvider

HOP_SIZE = 256
REQUIRED_SAMPLE_RATE = 16000


class TenVadProvider(VADProvider):
    name = "ten_vad"
    frame_hop_ms = HOP_SIZE / REQUIRED_SAMPLE_RATE * 1000.0

    def __init__(self):
        self._vad = TenVad(hop_size=HOP_SIZE)
        self._remainder = np.empty(0, dtype=np.int16)

    def score(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate != REQUIRED_SAMPLE_RATE:
            raise ValueError(
                f"TEN VAD requires {REQUIRED_SAMPLE_RATE} Hz audio, got {sample_rate}"
            )
        if pcm.dtype != np.int16:
            raise ValueError(f"TEN VAD requires int16 PCM, got {pcm.dtype}")

        samples = (
            np.concatenate([self._remainder, pcm]) if self._remainder.size else pcm
        )
        n_frames = samples.size // HOP_SIZE
        self._remainder = samples[n_frames * HOP_SIZE :]

        probabilities = np.empty(n_frames, dtype=np.float32)
        for i in range(n_frames):
            frame = samples[i * HOP_SIZE : (i + 1) * HOP_SIZE]
            probabilities[i], _ = self._vad.process(frame)
        return probabilities
