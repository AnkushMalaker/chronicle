"""
Fish Speech TTS synthesizer — pure HTTP client to fish-speech's native API server.

The fish-speech server is started by startup.py before this service runs.
This synthesizer only communicates via HTTP to the local sidecar.

Supports emotion/prosody control via inline tags like [laugh], [whispers], [super happy].
"""

import asyncio
import base64
import io
import logging
import os
from typing import Optional

import requests
import soundfile as sf

logger = logging.getLogger(__name__)

# Fish-speech internal API (started by startup.py)
_FISH_API_HOST = "127.0.0.1"
_FISH_API_PORT = 8080


class FishSpeechSynthesizer:
    """
    HTTP-client synthesizer that delegates to fish-speech's native API server.

    The server is already running (started by startup.py) when this class
    is instantiated. No subprocess management needed.
    """

    def __init__(self, model_id: Optional[str] = None):
        self.model_id = model_id or os.getenv("TTS_MODEL", "fishaudio/s2-pro")
        self._is_loaded = False
        self._lock = asyncio.Lock()
        self._sample_rate: Optional[int] = None

        logger.info(f"FishSpeechSynthesizer initialized: model={self.model_id}")

    def initialize(self) -> None:
        """Verify the fish-speech server is running and detect sample rate."""
        if self._is_loaded:
            return

        base_url = f"http://{_FISH_API_HOST}:{_FISH_API_PORT}"

        # Verify server is reachable
        try:
            resp = requests.get(f"{base_url}/v1/health", timeout=5)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(
                f"Fish-speech server not reachable at {base_url}: {e}"
            ) from e

        # Detect sample rate from a short synthesis
        self._sample_rate = self._detect_sample_rate()
        self._is_loaded = True
        logger.info(f"Fish Speech ready (sample_rate={self._sample_rate})")

    def _detect_sample_rate(self) -> int:
        """Detect sample rate from a short synthesis."""
        resp = requests.post(
            f"http://{_FISH_API_HOST}:{_FISH_API_PORT}/v1/tts",
            json={"text": "Hi.", "format": "wav", "streaming": False},
            timeout=60,
        )
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        data, sr = sf.read(buf)
        return sr

    async def synthesize(
        self,
        text: str,
        reference_audio_path: Optional[str] = None,
        reference_text: Optional[str] = None,
        **kwargs,
    ) -> tuple[bytes, int]:
        """
        Synthesize speech from text.

        Args:
            text: Text to synthesize. Supports inline emotion tags like [laugh], [whispers].
            reference_audio_path: Path to reference audio for voice cloning.
            reference_text: Transcript of the reference audio.
            **kwargs: Generation params passed to fish-speech API
                (temperature, top_p, repetition_penalty, seed, max_new_tokens).

        Returns:
            Tuple of (wav_bytes, sample_rate).
        """
        if not self._is_loaded:
            raise RuntimeError("Not initialized. Call initialize() first.")

        logger.info(f"Synthesizing: {len(text)} chars, params: {kwargs}")

        async with self._lock:
            loop = asyncio.get_event_loop()
            wav_bytes = await loop.run_in_executor(
                None,
                self._synthesize_sync,
                text,
                reference_audio_path,
                reference_text,
                kwargs,
            )

        return wav_bytes, self._sample_rate

    def _synthesize_sync(
        self,
        text: str,
        reference_audio_path: Optional[str],
        reference_text: Optional[str],
        gen_kwargs: Optional[dict] = None,
    ) -> bytes:
        """Synchronous synthesis via HTTP to fish-speech API."""
        payload: dict = {
            "text": text,
            "format": "wav",
            "streaming": False,
        }

        # Pass through generation parameters
        if gen_kwargs:
            for key in ("temperature", "top_p", "repetition_penalty", "seed", "max_new_tokens"):
                if key in gen_kwargs:
                    payload[key] = gen_kwargs[key]

        if reference_audio_path and reference_text:
            with open(reference_audio_path, "rb") as f:
                audio_bytes = f.read()
            payload["references"] = [
                {
                    "audio": base64.b64encode(audio_bytes).decode(),
                    "text": reference_text,
                }
            ]

        resp = requests.post(
            f"http://{_FISH_API_HOST}:{_FISH_API_PORT}/v1/tts",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.content

    @property
    def sample_rate(self) -> int:
        return self._sample_rate or 44100

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded
