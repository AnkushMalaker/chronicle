"""
TADA TTS synthesizer implementation.

Uses HumeAI's TADA model for text-to-speech with zero-shot voice cloning.
TADA uses 1:1 token alignment between text and audio, eliminating hallucinations.
"""

import asyncio
import io
import logging
import os
import wave
from typing import Optional

import torch
import torchaudio

logger = logging.getLogger(__name__)

# TADA generates at 24kHz
TADA_SAMPLE_RATE = 24000


class TadaSynthesizer:
    """
    Synthesizer using HumeAI TADA model.

    Supports:
    - HumeAI/tada-1b (English, ~4-5GB VRAM)
    - HumeAI/tada-3b-ml (Multilingual: ar, zh, de, es, fr, it, ja, pl, pt)

    Environment variables:
        TTS_MODEL: Model identifier (default: HumeAI/tada-1b)
    """

    def __init__(self, model_id: Optional[str] = None):
        self.model_id = model_id or os.getenv("TTS_MODEL", "HumeAI/tada-1b")
        self.model = None
        self.encoder = None
        self._is_loaded = False
        self._lock = asyncio.Lock()
        self._device = "cuda" if torch.cuda.is_available() else "cpu"

        logger.info(
            f"TadaSynthesizer initialized: model={self.model_id}, device={self._device}"
        )

    def load_model(self) -> None:
        """Load the TADA model and encoder."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        logger.info(f"Loading TADA model: {self.model_id}")

        # Lazy import: tada.modules pulls in the model weights/deps and is only
        # needed once model loading starts.
        from tada.modules.encoder import Encoder
        from tada.modules.tada import TadaForCausalLM

        # Determine language for encoder (multilingual model)
        language = os.getenv("TTS_LANGUAGE", None)
        encoder_kwargs = {}
        if language:
            encoder_kwargs["language"] = language

        self.encoder = Encoder.from_pretrained(
            "HumeAI/tada-codec", subfolder="encoder", **encoder_kwargs
        ).to(self._device)

        self.model = TadaForCausalLM.from_pretrained(self.model_id).to(self._device)

        self._is_loaded = True
        logger.info("TADA model loaded successfully")

    async def synthesize(
        self,
        text: str,
        reference_audio_path: Optional[str] = None,
        reference_text: Optional[str] = None,
    ) -> tuple[bytes, int]:
        """
        Synthesize speech from text.

        Args:
            text: Text to synthesize into speech.
            reference_audio_path: Path to reference audio for voice cloning.
            reference_text: Transcript of the reference audio (required if reference provided).

        Returns:
            Tuple of (wav_bytes, sample_rate).
        """
        if not self._is_loaded or self.model is None or self.encoder is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        logger.info(f"Synthesizing: {len(text)} chars")

        async with self._lock:
            loop = asyncio.get_event_loop()
            wav_bytes = await loop.run_in_executor(
                None,
                self._synthesize_sync,
                text,
                reference_audio_path,
                reference_text,
            )

        return wav_bytes, TADA_SAMPLE_RATE

    def _synthesize_sync(
        self,
        text: str,
        reference_audio_path: Optional[str],
        reference_text: Optional[str],
    ) -> bytes:
        """Synchronous synthesis (runs in thread pool)."""
        with torch.no_grad():
            prompt = None

            if reference_audio_path and reference_text:
                audio, sample_rate = torchaudio.load(reference_audio_path)
                audio = audio.to(self._device)
                prompt = self.encoder(
                    audio, text=[reference_text], sample_rate=sample_rate
                )

            output = self.model.generate(
                prompt=prompt,
                text=text,
            )

        # GenerationOutput.audio is a list of tensors (one per batch item)
        audio_tensor = output.audio[0]
        return self._tensor_to_wav_bytes(audio_tensor)

    def _tensor_to_wav_bytes(self, audio_tensor: torch.Tensor) -> bytes:
        """Convert a torch audio tensor to WAV bytes."""
        # Ensure CPU and correct shape
        audio = audio_tensor.cpu()
        if audio.dim() == 1:
            audio = audio.unsqueeze(0)

        # Normalize to [-1, 1] range if needed
        if audio.abs().max() > 1.0:
            audio = audio / audio.abs().max()

        # Convert to 16-bit PCM
        audio_int16 = (audio * 32767).to(torch.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(TADA_SAMPLE_RATE)
            wf.writeframes(audio_int16.numpy().tobytes())

        return buf.getvalue()

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded
