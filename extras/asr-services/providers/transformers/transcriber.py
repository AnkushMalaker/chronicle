"""
Transformers transcriber implementation.

Uses HuggingFace Transformers pipeline for standard Whisper ASR models.
For VibeVoice-ASR, use the dedicated vibevoice provider instead.
"""

import logging
import os
from typing import Optional

import torch
from common.response_models import Segment, TranscriptionResult, Word

logger = logging.getLogger(__name__)


class TransformersTranscriber:
    """
    Transcriber using HuggingFace Transformers pipeline.

    Supports standard Whisper models (openai/whisper-*) and fine-tuned variants.
    For VibeVoice-ASR with speaker diarization, use the dedicated vibevoice provider.

    Environment variables:
        ASR_MODEL: Model identifier (default: openai/whisper-large-v3)
        USE_FLASH_ATTENTION: Enable Flash Attention 2 (default: false)
        DEVICE: Device to use (default: cuda)
        TORCH_DTYPE: Torch dtype (default: float16)
    """

    def __init__(self, model_id: Optional[str] = None):
        """
        Initialize the transformers transcriber.

        Args:
            model_id: Model identifier. If None, reads from ASR_MODEL env var.
        """
        self.model_id = model_id or os.getenv("ASR_MODEL", "openai/whisper-large-v3")
        self.use_flash_attn = (
            os.getenv("USE_FLASH_ATTENTION", "false").lower() == "true"
        )
        self.device = os.getenv(
            "DEVICE", "cuda" if torch.cuda.is_available() else "cpu"
        )
        self.torch_dtype_str = os.getenv("TORCH_DTYPE", "float16")
        self.mixed_code = os.getenv("MIXED_CODE", "false").lower() == "true"

        # Determine torch dtype
        dtype_map = {
            "float16": torch.float16,
            "float32": torch.float32,
            "bfloat16": torch.bfloat16,
        }
        self.torch_dtype = dtype_map.get(self.torch_dtype_str, torch.float16)

        # Model components (initialized in load_model)
        self.model = None
        self.processor = None
        self.pipeline = None
        self._is_loaded = False

        logger.info(
            f"TransformersTranscriber initialized: "
            f"model={self.model_id}, device={self.device}, "
            f"dtype={self.torch_dtype_str}, flash_attn={self.use_flash_attn}"
        )

    def load_model(self) -> None:
        """Load the ASR model."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        logger.info(f"Loading transformers model: {self.model_id}")

        # Lazy import: transformers is heavy and only needed once model loading starts.
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

        # Load processor
        self.processor = AutoProcessor.from_pretrained(self.model_id)

        # Model kwargs
        model_kwargs = {
            "torch_dtype": self.torch_dtype,
            "low_cpu_mem_usage": True,
        }

        if self.use_flash_attn:
            model_kwargs["attn_implementation"] = "flash_attention_2"

        # Load model
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
            self.model_id, **model_kwargs
        )

        if self.device == "cuda":
            self.model = self.model.to(self.device)

        # Create pipeline
        self.pipeline = pipeline(
            "automatic-speech-recognition",
            model=self.model,
            tokenizer=self.processor.tokenizer,
            feature_extractor=self.processor.feature_extractor,
            torch_dtype=self.torch_dtype,
            device=self.device if self.device != "cpu" else -1,
        )

        self._is_loaded = True
        logger.info("Whisper pipeline created and ready")

    def supports_word_timestamps(self) -> bool:
        """Check if model has alignment heads for word-level timestamps."""
        if self.model is None:
            return False
        config = getattr(self.model, "config", None)
        if config is None:
            return False
        alignment_heads = getattr(config, "alignment_heads", None)
        return alignment_heads is not None

    def transcribe(
        self,
        audio_file_path: str,
        language: Optional[str] = None,
        return_timestamps: bool = True,
        word_timestamps: bool = True,
    ) -> TranscriptionResult:
        """
        Transcribe audio file.

        Args:
            audio_file_path: Path to audio file
            language: Language code (None for auto-detect)
            return_timestamps: Whether to return chunk-level timestamps
            word_timestamps: Whether to return word-level timestamps (requires alignment heads)

        Returns:
            TranscriptionResult with text, words, and segments
        """
        if not self._is_loaded:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        logger.info(f"Transcribing: {audio_file_path}")

        # Pipeline options
        generate_kwargs = {}
        if self.mixed_code:
            if not language:
                raise ValueError("MIXED_CODE=true requires LANGUAGE=hi or LANGUAGE=en")
            ids = self.processor.tokenizer.convert_tokens_to_ids
            mixed_code_ids = self.processor.tokenizer(
                "<|mixedcode|>", add_special_tokens=False
            ).input_ids
            prompt = [
                ids("<|startoftranscript|>"),
                ids(f"<|{language}|>"),
                *mixed_code_ids,
                ids("<|transcribe|>"),
            ]
            generate_kwargs["decoder_input_ids"] = torch.tensor(
                [prompt], device=self.device
            )
        elif language:
            generate_kwargs["language"] = language

        # Determine timestamp mode
        if return_timestamps and word_timestamps:
            ts_mode = "word"
        elif return_timestamps:
            ts_mode = True
        else:
            ts_mode = False

        # Run transcription
        result = self.pipeline(
            audio_file_path,
            return_timestamps=ts_mode,
            generate_kwargs=generate_kwargs or {},
        )

        # Debug: log raw pipeline output structure
        logger.info(f"Pipeline result keys: {list(result.keys())}")
        chunks = result.get("chunks", [])
        logger.info(f"Pipeline returned {len(chunks)} chunks, ts_mode={ts_mode}")
        if chunks:
            logger.info(f"First chunk: {chunks[0]}")
            if len(chunks) > 1:
                logger.info(f"Second chunk: {chunks[1]}")

        # Parse result
        text = result.get("text", "")
        all_words = []
        all_segments = []

        # Process chunks (timestamps)
        chunks = result.get("chunks", [])
        for chunk in chunks:
            timestamp = chunk.get("timestamp", (0.0, 0.0))
            start_time = timestamp[0] if timestamp[0] is not None else 0.0
            end_time = timestamp[1] if timestamp[1] is not None else start_time

            chunk_text = chunk.get("text", "").strip()
            if chunk_text:
                word = Word(
                    word=chunk_text,
                    start=start_time,
                    end=end_time,
                    confidence=None,
                )
                all_words.append(word)

        # Create single segment if we have text
        if text:
            end_time = all_words[-1].end if all_words else 0.0
            all_segments.append(
                Segment(
                    text=text,
                    start=0.0,
                    end=end_time,
                )
            )

        logger.info(
            f"Transcription complete: {len(text)} chars, {len(all_words)} words"
        )

        return TranscriptionResult(
            text=text,
            words=all_words,
            segments=all_segments,
        )

    @property
    def is_loaded(self) -> bool:
        """Return True if model is loaded."""
        return self._is_loaded
