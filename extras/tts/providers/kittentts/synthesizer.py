"""
KittenTTS synthesizer implementation.

KittenTTS is an ultra-lightweight (~25MB) ONNX text-to-speech model that runs
entirely on CPU — no GPU, no API key. It uses a fixed set of preset voices
(no zero-shot cloning), so reference audio is ignored.
"""

import asyncio
import io
import logging
import os
import re
import unicodedata
import wave
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# KittenTTS generates at 24kHz.
KITTEN_SAMPLE_RATE = 24000

# Emoji / pictographic ranges. KittenTTS verbalizes these (e.g. "thumbs up"),
# so we strip them before synthesis. This is KittenTTS-specific on purpose:
# other providers may use emoji as prosody/context, so we don't strip globally.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols, pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F000-\U0001F0FF"  # mahjong, dominoes, playing cards
    "\U00002190-\U000021FF"  # arrows
    "\U00002B00-\U00002BFF"  # misc symbols and arrows (stars, etc.)
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U0000200D"  # zero-width joiner (compound emoji)
    "\U000020E3"  # combining enclosing keycap
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    """
    Remove emoji and pictographic symbols from text so KittenTTS doesn't
    read out their names. Collapses any whitespace left behind.
    """
    cleaned = _EMOJI_PATTERN.sub("", text)
    # Drop any remaining standalone symbol/pictographic codepoints not caught above.
    cleaned = "".join(ch for ch in cleaned if unicodedata.category(ch) != "So")
    # Collapse the gaps the removals leave (e.g. "great 👍 job" -> "great job").
    return re.sub(r"\s{2,}", " ", cleaned).strip()


class KittenSynthesizer:
    """
    Synthesizer using the KittenTTS local ONNX model.

    Environment variables:
        TTS_MODEL: Model identifier (default: KittenML/kitten-tts-mini-0.8;
            also: kitten-tts-micro-0.8 (41MB), kitten-tts-nano-0.8-int8 (25MB))
        TTS_VOICE: Preset voice name (default: Jasper). Available:
            Bella, Jasper, Luna, Bruno, Rosie, Hugo, Kiki, Leo
        TTS_SPEED: Speech speed multiplier (default: 1.0)
    """

    def __init__(
        self,
        model_id: Optional[str] = None,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
    ):
        self.model_id = model_id or os.getenv(
            "TTS_MODEL", "KittenML/kitten-tts-mini-0.8"
        )
        self.voice = voice or os.getenv("TTS_VOICE", "Jasper")
        self.speed = float(
            speed if speed is not None else os.getenv("TTS_SPEED", "1.0")
        )
        self.model = None
        self._is_loaded = False
        self._lock = asyncio.Lock()

        logger.info(
            f"KittenSynthesizer initialized: model={self.model_id}, "
            f"voice={self.voice}, speed={self.speed}"
        )

    def load_model(self) -> None:
        """Load the KittenTTS ONNX model (downloads to HF cache on first use)."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        # Lazy import: kittentts is only needed once model loading starts.
        from kittentts import KittenTTS

        # KittenTTS 0.8.x takes the HF model id and self-wires espeak via
        # espeakng_loader (no system espeak-ng needed). Model caches to HF_HOME.
        logger.info(f"Loading KittenTTS model: {self.model_id}")
        self.model = KittenTTS(self.model_id)
        available = getattr(self.model, "available_voices", None)
        if available:
            logger.info(f"Available voices: {available}")
            if self.voice not in available:
                logger.warning(
                    f"Configured voice '{self.voice}' not in available voices; "
                    f"falling back to '{available[0]}'"
                )
                self.voice = available[0]
        self._is_loaded = True
        logger.info("KittenTTS model loaded successfully")

    async def synthesize(
        self,
        text: str,
        reference_audio_path: Optional[str] = None,
        reference_text: Optional[str] = None,
        **kwargs,
    ) -> tuple[bytes, int]:
        """
        Synthesize speech from text.

        KittenTTS uses preset voices, so reference_audio_path/reference_text are
        ignored. The voice and speed come from config; a request may override them
        via the ``voice``/``speed`` kwargs.
        """
        if not self._is_loaded or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        voice = kwargs.get("voice") or self.voice
        speed = float(kwargs.get("speed") or self.speed)

        logger.info(f"Synthesizing {len(text)} chars (voice={voice}, speed={speed})")

        async with self._lock:
            loop = asyncio.get_event_loop()
            wav_bytes = await loop.run_in_executor(
                None, self._synthesize_sync, text, voice, speed
            )

        return wav_bytes, KITTEN_SAMPLE_RATE

    def _synthesize_sync(self, text: str, voice: str, speed: float) -> bytes:
        """Synchronous synthesis (runs in a thread pool)."""
        clean_text = strip_emoji(text)
        if clean_text != text:
            logger.info("Stripped emoji from text before KittenTTS synthesis")
        if not clean_text:
            raise ValueError(
                "Text is empty after stripping emoji; nothing to synthesize"
            )
        audio = self.model.generate(clean_text, voice=voice, speed=speed)
        return self._to_wav_bytes(audio)

    def _to_wav_bytes(self, audio) -> bytes:
        """Convert a float32 numpy waveform to 16-bit PCM WAV bytes."""
        samples = np.asarray(audio, dtype=np.float32).flatten()

        # Normalize to [-1, 1] if the model returns out-of-range values.
        peak = float(np.abs(samples).max()) if samples.size else 0.0
        if peak > 1.0:
            samples = samples / peak

        pcm = (samples * 32767.0).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(KITTEN_SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())

        return buf.getvalue()

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded
