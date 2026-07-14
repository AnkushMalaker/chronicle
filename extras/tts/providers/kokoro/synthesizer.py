"""
Kokoro TTS synthesizer implementation.

Kokoro-82M is a lightweight (82M param) StyleTTS2/ISTFTNet model with fixed
preset voices (no zero-shot cloning). It runs comfortably under ~1GB VRAM on
GPU (and on CPU), making it the quality-per-VRAM sweet spot for short replies.

Environment variables:
    TTS_MODEL: HuggingFace repo id (default: hexgrad/Kokoro-82M)
    TTS_VOICE: Preset voice name (default: af_heart)
    TTS_LANG_CODE: Kokoro language code (default: a = American English)
    TTS_SPEED: Speech speed multiplier (default: 1.0)
"""

import asyncio
import io
import logging
import os
import wave
from typing import Optional

import numpy as np
import torch

logger = logging.getLogger(__name__)

# Kokoro generates at 24kHz.
KOKORO_SAMPLE_RATE = 24000


class KokoroSynthesizer:
    """Synthesizer using the Kokoro-82M model via the ``kokoro`` package."""

    def __init__(
        self,
        model_id: Optional[str] = None,
        voice: Optional[str] = None,
        lang_code: Optional[str] = None,
        speed: Optional[float] = None,
    ):
        self.model_id = model_id or os.getenv("TTS_MODEL", "hexgrad/Kokoro-82M")
        self.voice = voice or os.getenv("TTS_VOICE", "af_heart")
        self.lang_code = lang_code or os.getenv("TTS_LANG_CODE", "a")
        self.speed = float(
            speed if speed is not None else os.getenv("TTS_SPEED", "1.0")
        )
        self.pipeline = None
        self._is_loaded = False
        self._lock = asyncio.Lock()
        self._device = "cuda" if torch.cuda.is_available() else "cpu"

        logger.info(
            f"KokoroSynthesizer initialized: model={self.model_id}, "
            f"voice={self.voice}, lang_code={self.lang_code}, "
            f"speed={self.speed}, device={self._device}"
        )

    def load_model(self) -> None:
        """Load the Kokoro pipeline (downloads weights to HF cache on first use)."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        # Lazy import: kokoro is only needed once model loading starts.
        from kokoro import KPipeline

        logger.info(f"Loading Kokoro pipeline: {self.model_id} ({self.lang_code})")
        self.pipeline = KPipeline(
            lang_code=self.lang_code,
            repo_id=self.model_id,
            device=self._device,
        )
        self._is_loaded = True
        logger.info("Kokoro pipeline loaded successfully")

    async def synthesize(
        self,
        text: str,
        reference_audio_path: Optional[str] = None,
        reference_text: Optional[str] = None,
        **kwargs,
    ) -> tuple[bytes, int]:
        """
        Synthesize speech from text.

        Kokoro uses preset voices, so reference_audio_path/reference_text are
        ignored. The voice and speed come from config; a request may override
        them via the ``voice``/``speed`` kwargs.
        """
        if not self._is_loaded or self.pipeline is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        voice = kwargs.get("voice") or self.voice
        speed = float(kwargs.get("speed") or self.speed)

        logger.info(f"Synthesizing {len(text)} chars (voice={voice}, speed={speed})")

        async with self._lock:
            loop = asyncio.get_event_loop()
            wav_bytes = await loop.run_in_executor(
                None, self._synthesize_sync, text, voice, speed
            )

        return wav_bytes, KOKORO_SAMPLE_RATE

    def _synthesize_sync(self, text: str, voice: str, speed: float) -> bytes:
        """Synchronous synthesis (runs in a thread pool)."""
        # KPipeline splits longer text into sentence chunks and yields one audio
        # tensor per chunk; concatenate them into a single waveform.
        chunks: list[np.ndarray] = []
        for _gs, _ps, audio in self.pipeline(text, voice=voice, speed=speed):
            if audio is None:
                continue
            if isinstance(audio, torch.Tensor):
                audio = audio.detach().cpu().numpy()
            chunks.append(np.asarray(audio, dtype=np.float32).flatten())

        if not chunks:
            raise ValueError("Kokoro produced no audio for the given text")

        samples = np.concatenate(chunks)
        return self._to_wav_bytes(samples)

    def _to_wav_bytes(self, samples: np.ndarray) -> bytes:
        """Convert a float32 numpy waveform to 16-bit PCM WAV bytes."""
        samples = np.asarray(samples, dtype=np.float32).flatten()

        # Normalize to [-1, 1] if the model returns out-of-range values.
        peak = float(np.abs(samples).max()) if samples.size else 0.0
        if peak > 1.0:
            samples = samples / peak

        pcm = (samples * 32767.0).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(KOKORO_SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())

        return buf.getvalue()

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded
