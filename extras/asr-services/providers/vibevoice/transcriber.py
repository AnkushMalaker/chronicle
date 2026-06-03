"""
VibeVoice ASR transcriber implementation.

Uses Microsoft's VibeVoice-ASR-HF model via native transformers (v5.3+).
Speaker diarization, timestamps, and multi-language support are built in.

For long audio files, automatically batches into overlapping windows and
stitches results together.

Batching config is loaded from config/defaults.yml (asr_services.vibevoice section),
overridden by config/config.yml, and can be further overridden by environment variables.

Environment variables:
    ASR_MODEL: HuggingFace model ID (default: microsoft/VibeVoice-ASR-HF)
    VIBEVOICE_ATTN_IMPL: Attention implementation (default: sdpa)
        - sdpa: Scaled dot product attention (default, most compatible)
        - flash_attention_2: Faster but requires flash-attn package
        - eager: Standard PyTorch attention
    DEVICE: Device to use (default: cuda)
    TORCH_DTYPE: Torch dtype (default: bfloat16, recommended for VibeVoice)
    MAX_NEW_TOKENS: Maximum tokens for generation (default: 8192)
    BATCH_THRESHOLD_SECONDS: Override batch threshold from config (env > config > 300)
    BATCH_DURATION_SECONDS: Override batch window size from config (env > config > 240)
    BATCH_OVERLAP_SECONDS: Override batch overlap from config (env > config > 30)
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import torch
from common.audio_utils import STANDARD_SAMPLE_RATE, load_audio_file
from common.batching import split_audio_file, stitch_transcription_results
from common.response_models import Segment, Speaker, TranscriptionResult
from omegaconf import OmegaConf

logger = logging.getLogger(__name__)


def load_vibevoice_config() -> dict:
    """Load asr_services.vibevoice config from config.yml/defaults.yml.

    Returns an empty dict only when neither config file exists.
    Raises on any load/parse error so misconfigurations are caught early.
    """
    config_dir = Path(os.getenv("CONFIG_DIR", "/app/config"))
    defaults_path = config_dir / "defaults.yml"
    config_path = config_dir / "config.yml"

    if not defaults_path.exists() and not config_path.exists():
        logger.info("No config files found in %s, using env/defaults", config_dir)
        return {}

    defaults = OmegaConf.load(defaults_path) if defaults_path.exists() else {}
    user_config = OmegaConf.load(config_path) if config_path.exists() else {}
    merged = OmegaConf.merge(defaults, user_config)

    asr_config = OmegaConf.select(
        merged, "asr_services.vibevoice", default=OmegaConf.create({})
    )
    resolved = OmegaConf.to_container(asr_config, resolve=True)
    logger.info(f"Loaded vibevoice config: {resolved}")
    return resolved


class VibeVoiceTranscriber:
    """
    Transcriber using Microsoft VibeVoice-ASR-HF via native transformers.

    Uses AutoProcessor + VibeVoiceAsrForConditionalGeneration from
    transformers >= 5.3.0. No external repository clone needed.

    Batching config priority: env vars > config/config.yml > config/defaults.yml > hardcoded.

    Environment variables:
        ASR_MODEL: Model identifier (default: microsoft/VibeVoice-ASR-HF)
        VIBEVOICE_ATTN_IMPL: Attention implementation (default: sdpa)
        DEVICE: Device to use (default: cuda)
        TORCH_DTYPE: Torch dtype (default: bfloat16)
        MAX_NEW_TOKENS: Max tokens for generation (default: 8192)
        BATCH_THRESHOLD_SECONDS: Override batch threshold from config
        BATCH_DURATION_SECONDS: Override batch window size from config
        BATCH_OVERLAP_SECONDS: Override batch overlap from config
    """

    def __init__(self, model_id: Optional[str] = None):
        self.model_id = model_id or os.getenv("ASR_MODEL", "microsoft/VibeVoice-ASR-HF")
        self.attn_impl = os.getenv("VIBEVOICE_ATTN_IMPL", "flash_attention_2")
        self.device = os.getenv(
            "DEVICE", "cuda" if torch.cuda.is_available() else "cpu"
        )

        if self.device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError(
                "DEVICE=cuda but torch.cuda.is_available() is False. "
                "This usually means (1) the container has a CUDA-only torch wheel on a non-NVIDIA host, "
                "or (2) the GPU device files/drivers are not available inside Docker. "
                f"torch={torch.__version__} cuda={torch.version.cuda!r} hip={torch.version.hip!r}. "
                "Fix: install the correct ROCm/CUDA torch wheel for your hardware, "
                "or set DEVICE=cpu."
            )

        self.max_new_tokens = int(os.getenv("MAX_NEW_TOKENS", "8192"))
        self.repetition_penalty = float(os.getenv("REPETITION_PENALTY", "1.1"))

        # Quantization config: "4bit", "8bit", or none ("" / "none" / "off" -> full precision).
        # Default is none: 4-bit NF4 causes repetition collapse on hard audio (see compose note).
        self.quantization = os.getenv("QUANTIZATION", "").lower().strip()
        if self.quantization in ("none", "off", "false", "no"):
            self.quantization = ""

        # Determine torch dtype
        torch_dtype_str = os.getenv("TORCH_DTYPE", "bfloat16")
        dtype_map = {
            "float16": torch.float16,
            "float32": torch.float32,
            "bfloat16": torch.bfloat16,
        }
        self.torch_dtype = dtype_map.get(torch_dtype_str, torch.bfloat16)

        # Batching config: config.yml > env vars > hardcoded defaults
        config = load_vibevoice_config()
        self.batch_threshold = float(
            os.getenv("BATCH_THRESHOLD_SECONDS")
            or config.get("batch_threshold_seconds", 300)
        )
        self.batch_duration = float(
            os.getenv("BATCH_DURATION_SECONDS")
            or config.get("batch_duration_seconds", 240)
        )
        self.batch_overlap = float(
            os.getenv("BATCH_OVERLAP_SECONDS")
            or config.get("batch_overlap_seconds", 30)
        )

        # LoRA adapter path (auto-loaded after base model if set)
        self.lora_adapter_path = os.getenv("LORA_ADAPTER_PATH") or None

        # Model components (initialized in load_model)
        self.model = None
        self.processor = None
        self._is_loaded = False
        self._has_lora = False

        logger.info(
            f"VibeVoiceTranscriber initialized: "
            f"model={self.model_id}, "
            f"device={self.device}, dtype={torch_dtype_str}, attn={self.attn_impl}, "
            f"quantization={self.quantization or 'none'}, "
            f"batch_threshold={self.batch_threshold}s"
        )

    def _build_quantization_config(self):
        """Build BitsAndBytesConfig for 4-bit or 8-bit quantization."""
        if not self.quantization:
            return None

        from transformers import BitsAndBytesConfig

        if self.quantization == "4bit":
            logger.info("Using 4-bit quantization (bitsandbytes NF4)")
            return BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=self.torch_dtype,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
            )
        elif self.quantization == "8bit":
            logger.info("Using 8-bit quantization (bitsandbytes)")
            return BitsAndBytesConfig(load_in_8bit=True)
        else:
            logger.warning(
                f"Unknown quantization '{self.quantization}', loading without quantization"
            )
            return None

    def load_model(self) -> None:
        """Load the VibeVoice ASR model via native transformers."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        logger.info(f"Loading VibeVoice model: {self.model_id}")

        from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration

        # Load processor
        logger.info("Loading processor...")
        self.processor = AutoProcessor.from_pretrained(self.model_id)

        # Build quantization config if requested
        quant_config = self._build_quantization_config()

        # Device mapping
        is_rocm = torch.version.hip is not None
        device_map = None
        if self.device == "cuda":
            if is_rocm:
                device_map = {"": "cuda:0"}
            else:
                device_map = "auto"

        load_kwargs = {
            "torch_dtype": self.torch_dtype,
            "device_map": device_map,
            "low_cpu_mem_usage": False if is_rocm else True,
        }
        # The acoustic/semantic tokenizer encoders don't support flash_attention_2,
        # so use a per-submodel dict: flash_attention_2 for the text decoder (Qwen2),
        # sdpa for the tokenizer encoders.
        if self.attn_impl == "flash_attention_2":
            load_kwargs["attn_implementation"] = {
                "text_config": "flash_attention_2",
                "acoustic_tokenizer_encoder_config": "eager",
                "semantic_tokenizer_encoder_config": "eager",
            }
        elif self.attn_impl and self.attn_impl != "sdpa":
            load_kwargs["attn_implementation"] = self.attn_impl
        if quant_config:
            load_kwargs["quantization_config"] = quant_config
            logger.info(f"Loading model with {self.quantization} quantization")
        else:
            logger.info("Loading model without quantization")

        self.model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
            self.model_id,
            **load_kwargs,
        )

        # Move to device when not using accelerate device_map
        if self.device == "cuda" and device_map is None and not quant_config:
            self.model = self.model.to(self.device)
            logger.info(f"Model moved to {self.device}")
        elif self.device != "cuda" and not quant_config:
            self.model = self.model.to(self.device)
            logger.info(f"Model moved to {self.device}")

        self.model.eval()

        # Enable deterministic CUDA operations for reproducible inference
        torch.backends.cudnn.deterministic = True
        torch.use_deterministic_algorithms(True, warn_only=True)
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        logger.info("Deterministic CUDA operations enabled")

        # Auto-load LoRA adapter if configured
        if self.lora_adapter_path and Path(self.lora_adapter_path).exists():
            logger.info(f"Auto-loading LoRA adapter from {self.lora_adapter_path}")
            self.load_lora_adapter(self.lora_adapter_path)

        self._is_loaded = True
        logger.info("VibeVoice model loaded successfully")

    def load_lora_adapter(self, adapter_path: str) -> None:
        """Load or replace a LoRA adapter on the base model."""
        from peft import PeftModel

        if self.model is None:
            raise RuntimeError("Base model not loaded. Call load_model() first.")

        if self._has_lora:
            logger.info("Merging existing LoRA adapter before loading new one")
            self.model = self.model.merge_and_unload()
            self._has_lora = False

        logger.info(f"Loading LoRA adapter from {adapter_path}")
        self.model = PeftModel.from_pretrained(self.model, adapter_path)
        self.model.eval()
        self._has_lora = True
        logger.info("LoRA adapter loaded successfully")

    def transcribe(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio file using VibeVoice with speaker diarization.

        For audio longer than batch_threshold, automatically splits into
        overlapping windows and stitches results together.

        Args:
            audio_file_path: Path to audio file
            context_info: Optional hot words / context string passed as prompt
                to guide recognition.

        Returns:
            TranscriptionResult with text, segments (with speakers), and speaker list
        """
        if not self._is_loaded or self.model is None or self.processor is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        audio_array, sr = load_audio_file(
            audio_file_path, target_rate=STANDARD_SAMPLE_RATE
        )
        duration = len(audio_array) / sr

        if duration > self.batch_threshold:
            logger.info(
                f"Audio is {duration:.1f}s (>{self.batch_threshold}s), using batched transcription"
            )
            return self._transcribe_batched(audio_file_path, hotwords=context_info)
        else:
            logger.info(f"Audio is {duration:.1f}s, using single-shot transcription")
            return self._transcribe_single(audio_file_path, context_info=context_info)

    def _transcribe_single(
        self,
        audio_file_path: str,
        context_info: Optional[str] = None,
    ) -> TranscriptionResult:
        """
        Transcribe a single audio file (or batch window).

        Args:
            audio_file_path: Path to audio file
            context_info: Optional hot words / context string passed as prompt.

        Returns:
            TranscriptionResult with text, segments (with speakers), and speaker list
        """
        logger.info(f"Transcribing: {audio_file_path}")
        if context_info:
            logger.info(f"With context: {context_info[:120]}")

        # Build inputs via the native transformers API
        request_kwargs = {"audio": audio_file_path}
        if context_info:
            request_kwargs["prompt"] = context_info

        inputs = self.processor.apply_transcription_request(**request_kwargs)

        # Move to model device and dtype
        model_device = next(self.model.parameters()).device
        inputs = inputs.to(model_device, self.torch_dtype)

        logger.info(f"Input shapes - input_ids: {inputs['input_ids'].shape}")

        # Generate transcription
        # Seed RNG before generate() for deterministic output.
        # The model's get_audio_features() injects VAE noise via torch.randn()
        # unconditionally (no training guard), so without a fixed seed the
        # noise differs each run and eventually flips an argmax in the decoder.
        logger.info("Generating transcription...")
        torch.manual_seed(42)
        torch.cuda.manual_seed_all(42)
        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
                repetition_penalty=self.repetition_penalty,
            )

        # Decode: skip input tokens, parse structured output
        generated_ids = output_ids[:, inputs["input_ids"].shape[1] :]
        try:
            parsed_segments = self.processor.decode(
                generated_ids, return_format="parsed"
            )[0]
        except (json.JSONDecodeError, IndexError, ValueError) as e:
            # Model output was truncated at max_new_tokens — fall back to raw text
            logger.warning(f"Structured decode failed ({e}), falling back to raw text")
            raw_text = (
                self.processor.decode(generated_ids, return_format="raw")[0]
                if hasattr(self.processor, "decode")
                else ""
            )
            audio_array, sr = load_audio_file(
                audio_file_path, target_rate=STANDARD_SAMPLE_RATE
            )
            duration = len(audio_array) / sr
            return TranscriptionResult(
                text=raw_text if isinstance(raw_text, str) else str(raw_text),
                words=[],
                segments=[
                    Segment(
                        text=raw_text if isinstance(raw_text, str) else str(raw_text),
                        start=0.0,
                        end=duration,
                        speaker=None,
                    )
                ],
                speakers=None,
                language=None,
                duration=duration,
            )

        logger.info(f"Parsed {len(parsed_segments)} segments")

        return self._map_to_result(parsed_segments)

    def _transcribe_batched(
        self,
        audio_file_path: str,
        hotwords: Optional[str] = None,
    ) -> TranscriptionResult:
        """
        Transcribe a long audio file by splitting into overlapping windows.

        Args:
            audio_file_path: Path to the full audio file
            hotwords: Optional hot words string passed through to each window

        Returns:
            Stitched TranscriptionResult from all windows
        """
        windows = split_audio_file(
            audio_file_path,
            batch_duration=self.batch_duration,
            overlap=self.batch_overlap,
        )

        batch_results = []

        for i, (temp_path, start_time, end_time) in enumerate(windows):
            try:
                logger.info(
                    f"Batch {i + 1}/{len(windows)}: [{start_time:.0f}s - {end_time:.0f}s]"
                )

                # No inter-window context to avoid repetition loops.
                # The 30s audio overlap + midpoint stitching handles continuity.
                result = self._transcribe_single(temp_path, context_info=hotwords)
                batch_results.append((result, start_time, end_time))
                logger.info(
                    f"Batch {i + 1} done: {len(result.segments)} segments, "
                    f"{len(result.text)} chars"
                )

            finally:
                os.unlink(temp_path)

        return stitch_transcription_results(
            batch_results, overlap_seconds=self.batch_overlap
        )

    def _transcribe_batched_with_progress(
        self,
        audio_file_path: str,
        hotwords: Optional[str] = None,
    ):
        """
        Transcribe a long audio file with progress reporting.

        Yields:
            {"type": "progress", "current": i, "total": n} after each window
            {"type": "result", ...} as the final item (TranscriptionResult.to_dict())
        """
        windows = split_audio_file(
            audio_file_path,
            batch_duration=self.batch_duration,
            overlap=self.batch_overlap,
        )

        batch_results = []

        for i, (temp_path, start_time, end_time) in enumerate(windows):
            try:
                logger.info(
                    f"Batch {i + 1}/{len(windows)}: [{start_time:.0f}s - {end_time:.0f}s]"
                )

                result = self._transcribe_single(temp_path, context_info=hotwords)
                batch_results.append((result, start_time, end_time))
                logger.info(
                    f"Batch {i + 1} done: {len(result.segments)} segments, "
                    f"{len(result.text)} chars"
                )

            finally:
                os.unlink(temp_path)

            yield {"type": "progress", "current": i + 1, "total": len(windows)}

        final = stitch_transcription_results(
            batch_results, overlap_seconds=self.batch_overlap
        )
        yield {"type": "result", **final.to_dict()}

    def supports_batch_progress(self, audio_duration: float) -> bool:
        """Return True if this audio is long enough to use batched transcription with progress."""
        return audio_duration > self.batch_threshold

    def _map_to_result(self, parsed_segments: list[dict]) -> TranscriptionResult:
        """
        Map native transformers parsed output to TranscriptionResult.

        parsed_segments is a list of dicts:
            [{"Start": 0.0, "End": 15.43, "Speaker": 0, "Content": "..."}, ...]

        Returns:
            TranscriptionResult with mapped data
        """
        segments = []
        speakers_map: dict[str, tuple[float, float]] = {}
        text_parts = []

        for seg_data in parsed_segments:
            text = seg_data.get("Content", "").strip()
            start = float(seg_data.get("Start", 0.0))
            end = float(seg_data.get("End", 0.0))
            speaker_raw = seg_data.get("Speaker")

            if speaker_raw is not None:
                speaker_id = f"Speaker {speaker_raw}"
            else:
                speaker_id = None

            if text:
                text_parts.append(text)

            segment = Segment(
                text=text,
                start=start,
                end=end,
                speaker=speaker_id,
            )
            segments.append(segment)

            # Track speaker time ranges
            if speaker_id:
                if speaker_id not in speakers_map:
                    speakers_map[speaker_id] = (start, end)
                else:
                    prev_start, prev_end = speakers_map[speaker_id]
                    speakers_map[speaker_id] = (
                        min(prev_start, start),
                        max(prev_end, end),
                    )

        # Build speaker list
        speakers = [
            Speaker(id=spk_id, start=times[0], end=times[1])
            for spk_id, times in speakers_map.items()
        ]

        full_text = " ".join(text_parts) if text_parts else ""

        duration = None
        if segments:
            duration = max(s.end for s in segments)

        logger.info(
            f"Transcription complete: {len(full_text)} chars, "
            f"{len(segments)} segments, {len(speakers)} speakers"
        )

        return TranscriptionResult(
            text=full_text,
            words=[],  # VibeVoice provides segment-level, not word-level timestamps
            segments=segments,
            speakers=speakers if speakers else None,
            language=None,  # VibeVoice auto-detects
            duration=duration,
        )

    @property
    def is_loaded(self) -> bool:
        """Return True if model is loaded."""
        return self._is_loaded
