"""
VibeVoice ASR transcriber implementation.

Uses Microsoft's VibeVoice-ASR model with speaker diarization capabilities.
VibeVoice is a speech-to-text model with built-in speaker diarization.

For long audio files, automatically batches into overlapping windows and
stitches results together. Context from each window is passed to the next
via VibeVoice's native context_info parameter.

Batching config is loaded from config/defaults.yml (asr_services.vibevoice section),
overridden by config/config.yml, and can be further overridden by environment variables.

Environment variables:
    ASR_MODEL: HuggingFace model ID (default: microsoft/VibeVoice-ASR)
    VIBEVOICE_LLM_MODEL: LLM backbone for processor (default: Qwen/Qwen2.5-7B)
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
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import torch
from common.audio_utils import STANDARD_SAMPLE_RATE, load_audio_file
from common.batching import (
    extract_context_tail,
    split_audio_file,
    stitch_transcription_results,
)
from common.response_models import Segment, Speaker, TranscriptionResult

logger = logging.getLogger(__name__)


def load_vibevoice_config() -> dict:
    """Load asr_services.vibevoice config from config.yml/defaults.yml."""
    try:
        from omegaconf import OmegaConf

        config_dir = Path(os.getenv("CONFIG_DIR", "/app/config"))
        defaults_path = config_dir / "defaults.yml"
        config_path = config_dir / "config.yml"

        if not defaults_path.exists() and not config_path.exists():
            return {}

        defaults = OmegaConf.load(defaults_path) if defaults_path.exists() else {}
        user_config = OmegaConf.load(config_path) if config_path.exists() else {}
        merged = OmegaConf.merge(defaults, user_config)

        asr_config = merged.get("asr_services", {}).get("vibevoice", {})
        resolved = OmegaConf.to_container(asr_config, resolve=True)
        logger.info(f"Loaded vibevoice config: {resolved}")
        return resolved
    except Exception as e:
        logger.warning(f"Failed to load config: {e}, using env/defaults")
        return {}


class VibeVoiceTranscriber:
    """
    Transcriber using Microsoft VibeVoice-ASR.

    VibeVoice provides speech-to-text with speaker diarization.
    It requires cloning the VibeVoice repository for the model and processor classes.

    Batching config priority: env vars > config/config.yml > config/defaults.yml > hardcoded.

    Environment variables:
        ASR_MODEL: Model identifier (default: microsoft/VibeVoice-ASR)
        VIBEVOICE_LLM_MODEL: LLM backbone (default: Qwen/Qwen2.5-7B)
        VIBEVOICE_ATTN_IMPL: Attention implementation (default: sdpa)
        DEVICE: Device to use (default: cuda)
        TORCH_DTYPE: Torch dtype (default: bfloat16)
        MAX_NEW_TOKENS: Max tokens for generation (default: 8192)
        BATCH_THRESHOLD_SECONDS: Override batch threshold from config
        BATCH_DURATION_SECONDS: Override batch window size from config
        BATCH_OVERLAP_SECONDS: Override batch overlap from config
    """

    def __init__(self, model_id: Optional[str] = None):
        """
        Initialize the VibeVoice transcriber.

        Args:
            model_id: Model identifier. If None, reads from ASR_MODEL env var.
        """
        self.model_id = model_id or os.getenv("ASR_MODEL", "microsoft/VibeVoice-ASR")
        self.llm_model = os.getenv("VIBEVOICE_LLM_MODEL", "Qwen/Qwen2.5-7B")
        self.attn_impl = os.getenv("VIBEVOICE_ATTN_IMPL", "sdpa")
        self.device = os.getenv("DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
        self.max_new_tokens = int(os.getenv("MAX_NEW_TOKENS", "8192"))

        # Quantization config: "4bit", "8bit", or "" (none)
        self.quantization = os.getenv("QUANTIZATION", "").lower().strip()

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
            os.getenv("BATCH_THRESHOLD_SECONDS") or config.get("batch_threshold_seconds", 300)
        )
        self.batch_duration = float(
            os.getenv("BATCH_DURATION_SECONDS") or config.get("batch_duration_seconds", 240)
        )
        self.batch_overlap = float(
            os.getenv("BATCH_OVERLAP_SECONDS") or config.get("batch_overlap_seconds", 30)
        )

        # LoRA adapter path (auto-loaded after base model if set)
        self.lora_adapter_path = os.getenv("LORA_ADAPTER_PATH") or None


        # Model components (initialized in load_model)
        self.model = None
        self.processor = None
        self._is_loaded = False
        self._has_lora = False
        self._vibevoice_repo_path: Optional[Path] = None

        logger.info(
            f"VibeVoiceTranscriber initialized: "
            f"model={self.model_id}, llm={self.llm_model}, "
            f"device={self.device}, dtype={torch_dtype_str}, attn={self.attn_impl}, "
            f"quantization={self.quantization or 'none'}, "
            f"batch_threshold={self.batch_threshold}s"
        )

    def _setup_vibevoice(self) -> None:
        """Set up VibeVoice repository and add to path."""
        logger.info("Setting up VibeVoice-ASR...")

        # Check for pre-cloned repo in Docker image first
        hf_home = Path(os.getenv("HF_HOME", "/models"))
        vibevoice_dir = hf_home / "vibevoice"

        # Fallback to user cache if not in HF_HOME
        if not vibevoice_dir.exists():
            cache_dir = Path.home() / ".cache/huggingface"
            vibevoice_dir = cache_dir / "vibevoice"

        if not vibevoice_dir.exists():
            logger.info("Cloning VibeVoice repository...")
            vibevoice_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "git",
                    "clone",
                    "https://github.com/microsoft/VibeVoice.git",
                    str(vibevoice_dir),
                ],
                check=True,
            )
            logger.info(f"VibeVoice repository cloned to {vibevoice_dir}")
        else:
            logger.info(f"VibeVoice repository found at {vibevoice_dir}")

        self._vibevoice_repo_path = vibevoice_dir

        # Add to path for imports
        if str(vibevoice_dir) not in sys.path:
            sys.path.insert(0, str(vibevoice_dir))
            logger.info(f"Added {vibevoice_dir} to sys.path")

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
            logger.warning(f"Unknown quantization '{self.quantization}', loading without quantization")
            return None

    def load_model(self) -> None:
        """Load the VibeVoice ASR model."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        logger.info(f"Loading VibeVoice model: {self.model_id}")

        # Setup repository and imports
        self._setup_vibevoice()

        # Import VibeVoice components
        try:
            from vibevoice.modular.modeling_vibevoice_asr import (
                VibeVoiceASRForConditionalGeneration,
            )
            from vibevoice.processor.vibevoice_asr_processor import (
                VibeVoiceASRProcessor,
            )

            logger.info("VibeVoice modules imported successfully")
        except ImportError as e:
            logger.error(f"Failed to import VibeVoice modules: {e}")
            raise RuntimeError(
                f"Failed to import VibeVoice modules. "
                f"Ensure the VibeVoice repository is properly cloned. Error: {e}"
            )

        # Load processor with LLM backbone
        logger.info(f"Loading processor with LLM backbone: {self.llm_model}")
        self.processor = VibeVoiceASRProcessor.from_pretrained(
            self.model_id,
            language_model_pretrained_name=self.llm_model,
        )

        # Build quantization config if requested
        quant_config = self._build_quantization_config()

        # Load model
        load_kwargs = {
            "torch_dtype": self.torch_dtype,
            "device_map": "auto" if self.device == "cuda" else None,
            "attn_implementation": self.attn_impl,
            "trust_remote_code": True,
        }
        if quant_config:
            load_kwargs["quantization_config"] = quant_config
            logger.info(f"Loading model with {self.quantization} quantization")
        else:
            logger.info(f"Loading model with attn_implementation={self.attn_impl}")

        self.model = VibeVoiceASRForConditionalGeneration.from_pretrained(
            self.model_id,
            **load_kwargs,
        )

        # Move to device (only needed if not using device_map and not quantized)
        if self.device != "cuda" and not quant_config:
            self.model = self.model.to(self.device)
            logger.info(f"Model moved to {self.device}")

        self.model.eval()

        # Auto-load LoRA adapter if configured
        if self.lora_adapter_path and Path(self.lora_adapter_path).exists():
            logger.info(f"Auto-loading LoRA adapter from {self.lora_adapter_path}")
            self.load_lora_adapter(self.lora_adapter_path)

        self._is_loaded = True
        logger.info("VibeVoice model loaded successfully")

    def load_lora_adapter(self, adapter_path: str) -> None:
        """Load or replace a LoRA adapter on the base model.

        If a LoRA adapter is already loaded, it is merged and unloaded first
        before applying the new adapter.

        Args:
            adapter_path: Path to the directory containing the LoRA adapter weights.
        """
        from peft import PeftModel

        if self.model is None:
            raise RuntimeError("Base model not loaded. Call load_model() first.")

        # If already has a LoRA adapter, merge it back into base weights first
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
        overlapping windows, transcribes each with context from the previous
        window, and stitches results together.

        Args:
            audio_file_path: Path to audio file
            context_info: Optional hot words / context string passed to the
                processor's context_info parameter to guide recognition.

        Returns:
            TranscriptionResult with text, segments (with speakers), and speaker list
        """
        if not self._is_loaded or self.model is None or self.processor is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        # Check duration to decide whether to batch

        audio_array, sr = load_audio_file(audio_file_path, target_rate=STANDARD_SAMPLE_RATE)
        duration = len(audio_array) / sr

        if duration > self.batch_threshold:
            logger.info(
                f"Audio is {duration:.1f}s (>{self.batch_threshold}s), using batched transcription"
            )
            return self._transcribe_batched(
                audio_file_path,
                hotwords=context_info,
            )
        else:
            logger.info(f"Audio is {duration:.1f}s, using single-shot transcription")
            return self._transcribe_single(audio_file_path, context_info=context_info)

    def _transcribe_single(
        self, audio_file_path: str, context: Optional[str] = None, context_info: Optional[str] = None
    ) -> TranscriptionResult:
        """
        Transcribe a single audio file (or batch window).

        Args:
            audio_file_path: Path to audio file
            context: Optional context text from previous batch window
                (continuity context for batched transcription).
            context_info: Optional hot words / context string from the caller
                (e.g. LangFuse asr.hot_words prompt).

        Returns:
            TranscriptionResult with text, segments (with speakers), and speaker list
        """
        logger.info(f"Transcribing: {audio_file_path}")
        if context:
            logger.info(f"With batch context ({len(context)} chars): ...{context[-80:]}")
        if context_info:
            logger.info(f"With hot words context: {context_info[:120]}")

        # Build combined context_info: hot words + batch continuity context
        combined_context = None
        parts = []
        if context_info:
            parts.append(context_info.strip())
        if context:
            parts.append(context.strip())
        if parts:
            combined_context = "\n".join(parts)

        # Process audio through processor (can take file paths directly)
        processor_kwargs = {
            "audio": [audio_file_path],
            "sampling_rate": None,
            "return_tensors": "pt",
            "padding": True,
            "add_generation_prompt": True,
        }
        if combined_context:
            processor_kwargs["context_info"] = combined_context

        inputs = self.processor(**processor_kwargs)

        # Move inputs to device
        model_device = next(self.model.parameters()).device
        inputs = {
            k: v.to(model_device) if isinstance(v, torch.Tensor) else v
            for k, v in inputs.items()
        }

        logger.info(f"Input shapes - input_ids: {inputs['input_ids'].shape}")

        # Generation config
        generation_config = {
            "max_new_tokens": self.max_new_tokens,
            "pad_token_id": self.processor.pad_id,
            "eos_token_id": self.processor.tokenizer.eos_token_id,
            "do_sample": False,  # Greedy decoding for consistency
        }

        # Generate transcription
        logger.info("Generating transcription...")
        with torch.no_grad():
            output_ids = self.model.generate(**inputs, **generation_config)

        # Decode output (skip input tokens)
        input_length = inputs["input_ids"].shape[1]
        generated_ids = output_ids[0, input_length:]

        # Remove eos tokens
        eos_positions = (generated_ids == self.processor.tokenizer.eos_token_id).nonzero(as_tuple=True)[0]
        if len(eos_positions) > 0:
            generated_ids = generated_ids[: eos_positions[0] + 1]

        raw_output = self.processor.decode(generated_ids, skip_special_tokens=True)
        logger.info(f"Raw output length: {len(raw_output)} chars")

        # Parse structured output using processor's post-processing
        try:
            segments = self.processor.post_process_transcription(raw_output)
            processed = {"raw_text": raw_output, "segments": segments}
            logger.info(f"Parsed {len(segments)} segments")
        except Exception as e:
            logger.warning(f"Failed to parse with post_process_transcription: {e}")
            # Fallback to our JSON parsing
            processed = self._parse_vibevoice_output(raw_output)

        # Map to TranscriptionResult
        return self._map_to_result(processed, raw_output)

    def _transcribe_batched(
        self,
        audio_file_path: str,
        hotwords: Optional[str] = None,
    ) -> TranscriptionResult:
        """
        Transcribe a long audio file by splitting into overlapping windows.

        Each window gets context from the previous window's transcript tail,
        passed via VibeVoice's native context_info parameter.

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
        prev_context = None

        for i, (temp_path, start_time, end_time) in enumerate(windows):
            try:
                logger.info(
                    f"Batch {i+1}/{len(windows)}: [{start_time:.0f}s - {end_time:.0f}s]"
                )

                result = self._transcribe_single(temp_path, context=prev_context, context_info=hotwords)
                batch_results.append((result, start_time, end_time))
                prev_context = extract_context_tail(result, max_chars=500)
                logger.info(
                    f"Batch {i+1} done: {len(result.segments)} segments, "
                    f"{len(result.text)} chars"
                )

            finally:
                os.unlink(temp_path)

        return stitch_transcription_results(batch_results, overlap_seconds=self.batch_overlap)

    def _parse_vibevoice_output(self, raw_output: str) -> dict:
        """
        Parse VibeVoice raw output to extract segments with speaker info.

        VibeVoice outputs JSON in the assistant response:
        <|im_start|>assistant
        [{"Start":0.0,"End":3.0,"Speaker":0,"Content":"..."}]<|im_end|>

        Args:
            raw_output: Raw decoded output from model

        Returns:
            Dict with 'raw_text' and 'segments' list
        """
        # DEBUG: Log actual output format for troubleshooting
        logger.info(f"Raw output preview (first 500 chars): {raw_output[:500]}")
        logger.info(f"Raw output preview (last 500 chars): {raw_output[-500:]}")

        # Extract JSON array from assistant response
        # Strategy: Find the outermost [ ] that contains valid JSON
        # Look for array starting with [{ which indicates segment objects
        json_match = re.search(r'\[\s*\{.*\}\s*\]', raw_output, re.DOTALL)

        if not json_match:
            logger.warning("Could not find JSON array in output, returning raw text only")
            logger.warning(f"Output does not match pattern [{{...}}], checking for other formats...")
            # Try alternate pattern: just find any array
            json_match = re.search(r'\[.*\]', raw_output, re.DOTALL)

        if not json_match:
            logger.warning("No JSON array found in output")
            return {"raw_text": raw_output, "segments": []}

        try:
            segments_raw = json.loads(json_match.group(0))
            logger.info(f"Parsed {len(segments_raw)} segments from JSON")

            # Convert to our expected format
            segments = []
            for seg in segments_raw:
                segments.append({
                    "text": seg.get("Content", ""),
                    "start": float(seg.get("Start", 0.0)),
                    "end": float(seg.get("End", 0.0)),
                    "speaker": seg.get("Speaker", 0),
                })

            return {"raw_text": raw_output, "segments": segments}

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Failed to parse JSON segments: {e}")
            return {"raw_text": raw_output, "segments": []}

    def _map_to_result(self, processed: dict, raw_output: str) -> TranscriptionResult:
        """
        Map VibeVoice output to TranscriptionResult.

        Args:
            processed: Post-processed output dict with segments
            raw_output: Raw decoded output

        Returns:
            TranscriptionResult with mapped data
        """
        segments = []
        speakers_map: dict[str, tuple[float, float]] = {}
        text_parts = []

        for seg_data in processed.get("segments", []):
            text = seg_data.get("text", "").strip()
            start = seg_data.get("start_time", seg_data.get("start", 0.0))
            end = seg_data.get("end_time", seg_data.get("end", 0.0))
            speaker_raw = seg_data.get("speaker_id", seg_data.get("speaker"))
            # Convert speaker to string, avoiding double-prefix from fallback parser
            if speaker_raw is None:
                speaker_id = None
            elif isinstance(speaker_raw, str) and speaker_raw.startswith("Speaker "):
                speaker_id = speaker_raw
            else:
                speaker_id = f"Speaker {speaker_raw}"

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

        # Use raw text if no segments parsed
        full_text = " ".join(text_parts) if text_parts else processed.get("raw_text", raw_output)

        # Calculate total duration
        duration = None
        if segments:
            duration = max(s.end for s in segments)

        logger.info(
            f"Transcription complete: {len(full_text)} chars, "
            f"{len(segments)} segments, {len(speakers)} speakers"
        )

        return TranscriptionResult(
            text=full_text,
            words=[],  # VibeVoice doesn't provide word-level timestamps
            segments=segments,
            speakers=speakers if speakers else None,
            language=None,  # VibeVoice auto-detects
            duration=duration,
        )

    def _load_audio_fallback(self, audio_path: str):
        """Fallback audio loading using torchaudio."""
        import torchaudio

        waveform, sample_rate = torchaudio.load(audio_path)

        # Resample to 16kHz if needed
        if sample_rate != 16000:
            resampler = torchaudio.transforms.Resample(sample_rate, 16000)
            waveform = resampler(waveform)

        # Convert to mono
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)

        return waveform.squeeze().numpy()

    @property
    def is_loaded(self) -> bool:
        """Return True if model is loaded."""
        return self._is_loaded
