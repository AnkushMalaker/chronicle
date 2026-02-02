"""
Faster-Whisper transcriber implementation.

Uses CTranslate2 backend for 4-6x faster inference than OpenAI Whisper.
"""

import logging
import os
from typing import Optional

from faster_whisper import WhisperModel

from common.response_models import Segment, TranscriptionResult, Word

logger = logging.getLogger(__name__)


class FasterWhisperTranscriber:
    """
    Transcriber using faster-whisper (CTranslate2 backend).

    Environment variables:
        ASR_MODEL: Model identifier (default: Systran/faster-whisper-large-v3)
        COMPUTE_TYPE: Quantization type (default: float16)
        DEVICE: Device to use (default: cuda)
        DEVICE_INDEX: GPU device index (default: 0)
    """

    def __init__(self, model_id: Optional[str] = None):
        """
        Initialize the faster-whisper transcriber.

        Args:
            model_id: Model identifier. If None, reads from ASR_MODEL env var.
        """
        self.model_id = model_id or os.getenv(
            "ASR_MODEL", "Systran/faster-whisper-large-v3"
        )
        self.compute_type = os.getenv("COMPUTE_TYPE", "float16")
        self.device = os.getenv("DEVICE", "cuda")
        self.device_index = int(os.getenv("DEVICE_INDEX", "0"))

        self.model: Optional[WhisperModel] = None
        self._is_loaded = False

        logger.info(
            f"FasterWhisperTranscriber initialized: "
            f"model={self.model_id}, compute_type={self.compute_type}, "
            f"device={self.device}"
        )

    def load_model(self) -> None:
        """Load the Whisper model."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        logger.info(f"Loading faster-whisper model: {self.model_id}")
        logger.info(f"Compute type: {self.compute_type}, Device: {self.device}")

        self.model = WhisperModel(
            self.model_id,
            device=self.device,
            device_index=self.device_index,
            compute_type=self.compute_type,
        )

        self._is_loaded = True
        logger.info("Model loaded successfully")

    def transcribe(
        self,
        audio_file_path: str,
        language: Optional[str] = None,
        task: str = "transcribe",
        beam_size: int = 5,
        word_timestamps: bool = True,
        vad_filter: bool = True,
    ) -> TranscriptionResult:
        """
        Transcribe audio file using faster-whisper.

        Args:
            audio_file_path: Path to audio file
            language: Language code (None for auto-detect)
            task: "transcribe" or "translate"
            beam_size: Beam size for decoding
            word_timestamps: Whether to compute word-level timestamps
            vad_filter: Whether to use VAD to filter out non-speech

        Returns:
            TranscriptionResult with text, words, and segments
        """
        if not self._is_loaded or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        logger.info(f"Transcribing: {audio_file_path}")

        # Run transcription
        segments_generator, info = self.model.transcribe(
            audio_file_path,
            language=language,
            task=task,
            beam_size=beam_size,
            word_timestamps=word_timestamps,
            vad_filter=vad_filter,
        )

        # Process segments
        all_text_parts = []
        all_words = []
        all_segments = []

        for segment in segments_generator:
            all_text_parts.append(segment.text.strip())

            # Create segment entry
            seg = Segment(
                text=segment.text.strip(),
                start=segment.start,
                end=segment.end,
            )
            all_segments.append(seg)

            # Extract word-level timestamps if available
            if word_timestamps and segment.words:
                for word_info in segment.words:
                    word = Word(
                        word=word_info.word.strip(),
                        start=word_info.start,
                        end=word_info.end,
                        confidence=word_info.probability,
                    )
                    all_words.append(word)

        # Combine text
        full_text = " ".join(all_text_parts)

        logger.info(
            f"Transcription complete: {len(full_text)} chars, "
            f"{len(all_words)} words, {len(all_segments)} segments"
        )
        logger.info(f"Detected language: {info.language} (prob: {info.language_probability:.2f})")

        return TranscriptionResult(
            text=full_text,
            words=all_words,
            segments=all_segments,
            language=info.language,
            duration=info.duration,
        )

    @property
    def is_loaded(self) -> bool:
        """Return True if model is loaded."""
        return self._is_loaded
