"""
NeMo transcriber implementation.

Uses NVIDIA NeMo ASR models (Parakeet, Canary, etc.) with native
timestamp support. NeMo handles long audio internally.
"""

import asyncio
import logging
import os
import re
from typing import List, Optional, cast

import numpy as np
import torch
from common.response_models import TranscriptionResult, Word

logger = logging.getLogger(__name__)

# Constants
NEMO_SAMPLE_RATE = 16000


def _parse_att_context_size(raw: str) -> List[int]:
    """Parse an ``att_context_size`` env string like ``"[56,3]"`` into ``[56, 3]``."""
    cleaned = raw.strip().strip("[]")
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"NEMO_ATT_CONTEXT_SIZE must be 'left,right' (got {raw!r})")
    return [int(parts[0]), int(parts[1])]


_LANG_TAG_RE = re.compile(r"\s*<[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?>\s*")


def _strip_lang_tags(text: str) -> str:
    """Remove the model's inline language tags (e.g. ``<en-US>``, ``<hi-IN>``).

    Nemotron 3.5 emits a language tag at each segment boundary; they're
    metadata, not transcript, so we drop them and tidy the spacing.
    """
    if not text:
        return text
    return _LANG_TAG_RE.sub(" ", text).strip()


def _extract_stream_text(hyps) -> str:
    """Pull transcription text out of a NeMo streaming hypothesis list."""
    if not hyps:
        return ""
    first = hyps[0]
    if hasattr(first, "text"):
        return _strip_lang_tags(first.text or "")
    if isinstance(first, str):
        return _strip_lang_tags(first)
    return ""


class NemoTranscriber:
    """
    Transcriber using NVIDIA NeMo ASR models.

    Supports:
    - nvidia/parakeet-tdt-0.6b-v3
    - nvidia/canary-1b
    - Other NeMo ASR models

    NeMo's transcribe() method handles long audio natively with word-level
    timestamps - no custom chunking required.

    Environment variables:
        ASR_MODEL: Model identifier (default: nvidia/parakeet-tdt-0.6b-v3)
    """

    def __init__(self, model_id: Optional[str] = None):
        """
        Initialize the NeMo transcriber.

        Args:
            model_id: Model identifier. If None, reads from ASR_MODEL env var.
        """
        self.model_id = model_id or os.getenv(
            "ASR_MODEL", "nvidia/parakeet-tdt-0.6b-v3"
        )

        # Cache-aware streaming context "[left,right]". Only configured when the
        # env var is explicitly set (cache-aware models like
        # nvidia/nemotron-3.5-asr-streaming-0.6b); left unset for plain offline
        # models (e.g. parakeet-tdt) so their behavior is unchanged.
        att_raw = os.getenv("NEMO_ATT_CONTEXT_SIZE", "").strip()
        self.att_context_size = _parse_att_context_size(att_raw) if att_raw else None

        # Language prompt for prompt-conditioned models (Nemotron 3.5). Ignored by
        # plain models (parakeet). A key in the model's prompt_dictionary; "auto"
        # is the language-agnostic prompt (best for code-switched / Hinglish
        # audio), or force one e.g. "en-US", "hi-IN", "es-ES".
        self.target_lang = os.getenv("NEMO_TARGET_LANG", "auto").strip() or "auto"

        self.model = None
        self._online_normalization = False
        self._is_loaded = False
        self._lock = asyncio.Lock()

        logger.info(
            f"NemoTranscriber initialized: model={self.model_id}, "
            f"att_context_size={self.att_context_size}"
        )

    def load_model(self) -> None:
        """Load the NeMo ASR model."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        logger.info(f"Loading NeMo ASR model: {self.model_id}")

        import nemo.collections.asr as nemo_asr

        self.model = cast(
            nemo_asr.models.ASRModel,
            nemo_asr.models.ASRModel.from_pretrained(model_name=self.model_id),
        )

        # Configure cache-aware streaming attention context when requested.
        if self.att_context_size is not None:
            if hasattr(self.model.encoder, "set_default_att_context_size"):
                self.model.encoder.set_default_att_context_size(self.att_context_size)
                logger.info(f"Set att_context_size={self.att_context_size}")
            else:
                logger.warning(
                    "NEMO_ATT_CONTEXT_SIZE set but encoder has no "
                    "set_default_att_context_size — model is not cache-aware; "
                    "streaming interims will use the windowed fallback."
                )

        # Online (per-chunk) feature normalization must match the model's training
        # normalization for cache-aware streaming to stay numerically stable.
        normalize = getattr(self.model.cfg.preprocessor, "normalize", None)
        self._online_normalization = normalize in ("per_feature", "all_features")

        self._is_loaded = True
        logger.info("Model loaded successfully")

    def _transcribe_via_streaming(self, audio_file_path: str) -> TranscriptionResult:
        """Offline transcription driven through the cache-aware streaming decoder.

        Loads the file as 16 kHz mono float32 and feeds it through a fresh
        streaming session in ~2 s chunks. Used for prompt-conditioned models
        whose NeMo offline file path is unavailable (see transcribe()).
        """
        import librosa

        samples, _ = librosa.load(audio_file_path, sr=NEMO_SAMPLE_RATE, mono=True)
        samples = samples.astype(np.float32)

        session = NemoStreamingSession(
            self.model,
            online_normalization=self._online_normalization,
            target_lang=self.target_lang,
        )
        # Offline: feed the whole signal in one append (the buffer still slices it
        # into model chunks internally) — no incremental mel-boundary effects.
        text = session.add_audio(samples)

        words = [
            Word(
                word=w["word"],
                start=w["start"],
                end=w["end"],
                confidence=w.get("confidence", 1.0),
            )
            for w in session.get_words()
        ]
        return TranscriptionResult(text=text, words=words, segments=[])

    def new_streaming_session(self) -> "NemoStreamingSession":
        """Create a per-connection cache-aware streaming decode session."""
        if not self._is_loaded or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")
        return NemoStreamingSession(
            self.model,
            online_normalization=self._online_normalization,
            target_lang=self.target_lang,
        )

    async def transcribe(self, audio_file_path: str) -> TranscriptionResult:
        """
        Transcribe audio file using NeMo.

        NeMo's transcribe() handles long audio natively with timestamps=True.
        No custom chunking is needed.

        Args:
            audio_file_path: Path to audio file (WAV format, 16kHz mono preferred)

        Returns:
            TranscriptionResult with text, words, and segments
        """
        if not self._is_loaded or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        logger.info(f"Transcribing: {audio_file_path}")

        # Prompt-conditioned models (Nemotron 3.5) can't use NeMo's offline
        # file-transcribe path: its Lhotse prompt-index dataset needs a per-cut
        # language that plain file inputs don't carry. Route offline through the
        # cache-aware streaming decoder instead (explicit set_inference_prompt),
        # so every path uses the same genuinely-streaming decode. Plain models
        # (parakeet) keep NeMo's native offline transcribe().
        if hasattr(self.model, "set_inference_prompt"):
            async with self._lock:
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._transcribe_via_streaming, audio_file_path
                )

        async with self._lock:
            with torch.no_grad():
                results = self.model.transcribe(
                    [audio_file_path], batch_size=1, timestamps=True
                )

        if not results or len(results) == 0:
            logger.warning("NeMo returned empty results")
            return TranscriptionResult(text="", words=[], segments=[])

        result = results[0]

        # Extract text
        if hasattr(result, "text") and result.text:
            text = result.text
        elif isinstance(result, str):
            text = result
        else:
            text = ""

        # Extract word-level timestamps - NeMo Parakeet format
        words = []
        if hasattr(result, "timestamp") and "word" in result.timestamp:
            for word_data in result.timestamp["word"]:
                word = Word(
                    word=word_data["word"],
                    start=word_data["start"],
                    end=word_data["end"],
                    confidence=1.0,
                )
                words.append(word)

        logger.info(f"Transcription complete: {len(text)} chars, {len(words)} words")

        return TranscriptionResult(
            text=text,
            words=words,
            segments=[],
        )

    @property
    def is_loaded(self) -> bool:
        """Return True if model is loaded."""
        return self._is_loaded


class NemoStreamingSession:
    """Per-connection cache-aware streaming decode state.

    Feed raw 16 kHz mono float32 audio via :meth:`add_audio`; it returns the
    cumulative transcription so far. Encoder caches and decoder hypotheses are
    threaded across calls so each new chunk only costs its own compute (this is
    the point of a cache-aware streaming model like Nemotron 3.5).
    """

    def __init__(
        self, model, online_normalization: bool = False, target_lang: str = "auto"
    ):
        from nemo.collections.asr.parts.utils.streaming_utils import (
            CacheAwareStreamingAudioBuffer,
        )

        self.model = model

        # Prompt-conditioned models (Nemotron 3.5) need the language prompt set
        # before any conformer_stream_step; plain models lack this method.
        if hasattr(model, "set_inference_prompt"):
            try:
                model.set_inference_prompt(target_lang)
            except Exception as e:
                logger.warning(
                    f"set_inference_prompt('{target_lang}') failed ({e}); "
                    "trying 'en-US'"
                )
                try:
                    model.set_inference_prompt("en-US")
                except Exception as e2:
                    logger.warning(
                        f"set_inference_prompt('en-US') also failed ({e2}); "
                        "streaming without an explicit language prompt"
                    )

        self.buffer = CacheAwareStreamingAudioBuffer(
            model=model, online_normalization=online_normalization
        )
        (
            self.cache_last_channel,
            self.cache_last_time,
            self.cache_last_channel_len,
        ) = model.encoder.get_initial_cache_state(batch_size=1)
        self.previous_hypotheses = None
        self.pred_out_stream = None
        self.text = ""
        self._first_step = True
        # -1 tells the buffer to start a new stream on the first append; it
        # returns the assigned id, which we reuse for subsequent appends.
        self._stream_id = -1
        # drop_extra_pre_encoded compensates for the pre-encoding overlap the
        # cache-aware buffer introduces; the first chunk has no prior cache.
        streaming_cfg = model.encoder.streaming_cfg
        drop = getattr(streaming_cfg, "drop_extra_pre_encoded", 0)
        self._drop_extra_pre_encoded = (
            drop[0] if isinstance(drop, (list, tuple)) else drop
        )

    def add_audio(self, samples: np.ndarray) -> str:
        """Append float32 samples and return the cumulative transcript text."""
        with torch.no_grad():
            # First append (empty buffer) must use -1 to create the stream; the
            # buffer leaves stream_id at -1 in that case, so pin it to 0 (this
            # session owns exactly one stream). Reusing -1 would add a NEW stream
            # on every append → batch grows → O(n^2) compute then a shape crash.
            self.buffer.append_audio(samples, stream_id=self._stream_id)
            self._stream_id = 0
            for chunk_audio, chunk_lengths in self.buffer:
                drop = 0 if self._first_step else self._drop_extra_pre_encoded
                (
                    self.pred_out_stream,
                    transcribed_texts,
                    self.cache_last_channel,
                    self.cache_last_time,
                    self.cache_last_channel_len,
                    self.previous_hypotheses,
                ) = self.model.conformer_stream_step(
                    processed_signal=chunk_audio,
                    processed_signal_length=chunk_lengths,
                    cache_last_channel=self.cache_last_channel,
                    cache_last_time=self.cache_last_time,
                    cache_last_channel_len=self.cache_last_channel_len,
                    keep_all_outputs=self.buffer.is_buffer_empty(),
                    previous_hypotheses=self.previous_hypotheses,
                    previous_pred_out=self.pred_out_stream,
                    drop_extra_pre_encoded=drop,
                    return_transcription=True,
                )
                self._first_step = False
                self.text = _extract_stream_text(transcribed_texts)
        return self.text

    def get_words(self) -> list:
        """Best-effort word timestamps from the current streaming hypothesis.

        Cache-aware RNNT hypotheses expose word timings only when the decoding
        strategy computes them; when unavailable this returns an empty list and
        the (correct) cumulative text still stands on its own.
        """
        hyps = self.previous_hypotheses
        if not hyps:
            return []
        hyp = hyps[0]
        ts = getattr(hyp, "timestamp", None)
        if not isinstance(ts, dict) or not ts.get("word"):
            return []
        out = []
        for w in ts["word"]:
            if not isinstance(w, dict):
                continue
            out.append(
                {
                    "word": w.get("word", ""),
                    "start": float(w.get("start", 0.0)),
                    "end": float(w.get("end", 0.0)),
                    "confidence": 1.0,
                }
            )
        return out
