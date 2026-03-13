"""
Qwen3-ASR transcriber implementation.

Uses Alibaba's Qwen3-ASR model served via vLLM for batch transcription.
Qwen3-ASR supports 52 languages with automatic language detection.

Audio is base64-encoded and sent to vLLM's OpenAI-compatible /v1/chat/completions
endpoint. The model returns text in "language X<asr_text>transcribed text</asr_text>"
format, which is parsed to extract both language and clean transcript.

Optionally integrates Qwen3ForcedAligner (from qwen-asr package) for word-level
timestamps when FORCED_ALIGNER_MODEL env var is set.

Environment variables:
    QWEN3_VLLM_URL: URL of the vLLM server (default: http://localhost:8000)
    ASR_MODEL: HuggingFace model ID (default: Qwen/Qwen3-ASR-1.7B)
    FORCED_ALIGNER_MODEL: HuggingFace model ID for ForcedAligner (optional)
"""

import asyncio
import base64
import io
import json
import logging
import os
import wave
from typing import Optional, Tuple

import httpx
from common.response_models import TranscriptionResult, Word

logger = logging.getLogger(__name__)

VLLM_TIMEOUT = float(os.getenv("QWEN3_VLLM_TIMEOUT", "120"))

# Optional ForcedAligner support (requires qwen-asr package)
HAS_FORCED_ALIGNER = False
try:
    from qwen_asr import Qwen3ForcedAligner

    HAS_FORCED_ALIGNER = True
except ImportError:
    pass


def _audio_to_wav_base64(audio_file_path: str) -> str:
    """Read an audio file and return its base64-encoded WAV representation.

    If the file is already WAV it is read directly. Otherwise soundfile is used
    to decode and re-encode as 16-bit 16 kHz mono WAV.
    """
    try:
        with wave.open(audio_file_path, "rb") as wf:
            # Already a valid WAV – just base64-encode the raw file bytes.
            with open(audio_file_path, "rb") as f:
                return base64.b64encode(f.read()).decode()
    except wave.Error:
        pass

    # Not a WAV – use soundfile to convert
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(audio_file_path, dtype="int16")

    # Mono conversion
    if data.ndim > 1:
        data = data.mean(axis=1).astype("int16")

    # Resample to 16 kHz if needed
    if sr != 16000:
        from common.audio_utils import load_audio_file

        audio_array, sr = load_audio_file(audio_file_path, target_rate=16000)
        data = (audio_array * 32767).astype("int16")
        sr = 16000

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(data.tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def detect_and_fix_repetitions(text: str, threshold: int = 20) -> str:
    """Detect and collapse excessive character/pattern repetitions.

    Ported from qwen_asr/inference/utils.py. Prevents degenerate model
    output where tokens or phrases repeat hundreds of times.
    """

    def fix_char_repeats(s: str, thresh: int) -> str:
        res = []
        i = 0
        n = len(s)
        while i < n:
            count = 1
            while i + count < n and s[i + count] == s[i]:
                count += 1
            if count > thresh:
                res.append(s[i])
            else:
                res.append(s[i : i + count])
            i += count
        return "".join(res)

    def fix_pattern_repeats(s: str, thresh: int, max_len: int = 20) -> str:
        n = len(s)
        min_repeat_chars = thresh * 2
        if n < min_repeat_chars:
            return s

        i = 0
        result = []
        found = False
        while i <= n - min_repeat_chars:
            found = False
            for k in range(1, max_len + 1):
                if i + k * thresh > n:
                    break

                pattern = s[i : i + k]
                valid = True
                for rep in range(1, thresh):
                    start_idx = i + rep * k
                    if s[start_idx : start_idx + k] != pattern:
                        valid = False
                        break

                if valid:
                    end_index = i + thresh * k
                    while (
                        end_index + k <= n and s[end_index : end_index + k] == pattern
                    ):
                        end_index += k
                    result.append(pattern)
                    result.append(fix_pattern_repeats(s[end_index:], thresh, max_len))
                    i = n
                    found = True
                    break

            if found:
                break
            else:
                result.append(s[i])
                i += 1

        if not found:
            result.append(s[i:])
        return "".join(result)

    text = fix_char_repeats(text, threshold)
    text = fix_pattern_repeats(text, threshold)
    return text


def _parse_qwen3_output(raw: str) -> Tuple[str, str]:
    """Parse Qwen3-ASR raw output into (language, text).

    Handles these output formats:
      - "language English<asr_text>hello world</asr_text>" → ("English", "hello world")
      - "language None<asr_text></asr_text>" → ("", "")  (silent audio)
      - Plain text without tags → ("", text)
    """
    if not raw:
        return "", ""

    s = raw.strip()
    if not s:
        return "", ""

    s = detect_and_fix_repetitions(s)

    tag = "<asr_text>"
    if tag not in s:
        # No tag — plain text fallback
        return "", s

    meta_part, text_part = s.split(tag, 1)

    # Strip closing tag if present
    end_tag = "</asr_text>"
    if text_part.endswith(end_tag):
        text_part = text_part[: -len(end_tag)]

    # Check for silent audio ("language None")
    if "language none" in meta_part.lower():
        t = text_part.strip()
        if not t:
            return "", ""
        # Model returned something despite "None" language
        return "", t

    # Extract language from "language X" prefix
    lang = ""
    for line in meta_part.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("language "):
            val = line[len("language ") :].strip()
            if val:
                lang = val
            break

    return lang, text_part.strip()


class Qwen3ASRTranscriber:
    """Batch transcriber that sends audio to a vLLM server running Qwen3-ASR."""

    def __init__(self, model_id: Optional[str] = None, vllm_url: Optional[str] = None):
        self.model_id = model_id or os.getenv("ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")
        self.vllm_url = (
            vllm_url or os.getenv("QWEN3_VLLM_URL", "http://localhost:8000")
        ).rstrip("/")
        self._client = httpx.AsyncClient(timeout=VLLM_TIMEOUT)
        self._aligner = None

        # Load ForcedAligner if configured
        aligner_model = os.getenv("FORCED_ALIGNER_MODEL")
        if aligner_model and HAS_FORCED_ALIGNER:
            try:
                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
                dtype = torch.bfloat16 if device == "cuda" else torch.float32
                logger.info(
                    f"Loading ForcedAligner: model={aligner_model}, device={device}, dtype={dtype}"
                )
                self._aligner = Qwen3ForcedAligner.from_pretrained(
                    aligner_model, device_map=device, dtype=dtype
                )
                logger.info("ForcedAligner loaded successfully")
            except Exception as e:
                logger.warning(f"Failed to load ForcedAligner: {e}")
                self._aligner = None
        elif aligner_model and not HAS_FORCED_ALIGNER:
            logger.warning(
                "FORCED_ALIGNER_MODEL is set but qwen-asr package is not installed. "
                "Install with: pip install qwen-asr"
            )

        logger.info(
            f"Qwen3ASRTranscriber: model={self.model_id}, vllm_url={self.vllm_url}, "
            f"aligner={'loaded' if self._aligner else 'none'}"
        )

    async def check_health(self) -> bool:
        """Check whether the vLLM server is reachable."""
        try:
            resp = await self._client.get(f"{self.vllm_url}/health")
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"vLLM health check failed: {e}")
            return False

    async def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe an audio file via vLLM /v1/chat/completions.

        Args:
            audio_file_path: Path to audio file (WAV preferred).
            context_info: Optional context / hot words (unused by Qwen3-ASR currently).

        Returns:
            TranscriptionResult with text, language, and optionally word timestamps.
        """
        logger.info(f"Transcribing: {audio_file_path}")

        audio_b64 = _audio_to_wav_base64(audio_file_path)
        audio_url = f"data:audio/wav;base64,{audio_b64}"

        # Build the chat completion request following Qwen3-ASR's expected format.
        # The model expects audio content only — no text instruction.
        payload = {
            "model": self.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "audio_url", "audio_url": {"url": audio_url}},
                    ],
                }
            ],
            "temperature": 0.0,
            "max_tokens": 4096,
        }

        resp = await self._client.post(
            f"{self.vllm_url}/v1/chat/completions",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"vLLM response keys: {list(data.keys())}")
        if "choices" not in data:
            logger.error(f"Unexpected vLLM response: {json.dumps(data)[:500]}")

        raw_text = data["choices"][0]["message"]["content"]
        logger.info(f"Raw output length: {len(raw_text)} chars")

        language, text = _parse_qwen3_output(raw_text)
        logger.info(f"Parsed: language={language!r}, text length={len(text)}")

        words = []

        # Run ForcedAligner for word timestamps if available
        if self._aligner and text:
            try:
                words = await self._run_aligner(audio_file_path, text, language)
                logger.info(f"ForcedAligner produced {len(words)} word timestamps")
            except Exception as e:
                logger.warning(
                    f"ForcedAligner failed, returning without timestamps: {e}"
                )

        return TranscriptionResult(
            text=text,
            words=words,
            segments=[],
            language=language or None,
        )

    async def _run_aligner(
        self, audio_file_path: str, text: str, language: str
    ) -> list[Word]:
        """Run ForcedAligner in a thread executor (it's sync/blocking).

        Returns list of Word objects with start/end timestamps.
        """
        loop = asyncio.get_event_loop()
        align_results = await loop.run_in_executor(
            None,
            self._aligner.align,
            audio_file_path,
            text,
            language or "English",
        )

        words = []
        if align_results:
            # align() returns List[ForcedAlignResult], each has .items: List[ForcedAlignItem]
            for result in align_results:
                for item in result.items:
                    words.append(
                        Word(word=item.text, start=item.start_time, end=item.end_time)
                    )

        return words
