"""
VibeVoice ASR transcriber implementation.

Uses Microsoft's VibeVoice-ASR model with speaker diarization capabilities.
VibeVoice is a speech-to-text model with built-in speaker diarization.

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

from common.response_models import Segment, Speaker, TranscriptionResult

logger = logging.getLogger(__name__)


class VibeVoiceTranscriber:
    """
    Transcriber using Microsoft VibeVoice-ASR.

    VibeVoice provides speech-to-text with speaker diarization.
    It requires cloning the VibeVoice repository for the model and processor classes.

    Environment variables:
        ASR_MODEL: Model identifier (default: microsoft/VibeVoice-ASR)
        VIBEVOICE_LLM_MODEL: LLM backbone (default: Qwen/Qwen2.5-7B)
        VIBEVOICE_ATTN_IMPL: Attention implementation (default: sdpa)
        DEVICE: Device to use (default: cuda)
        TORCH_DTYPE: Torch dtype (default: bfloat16)
        MAX_NEW_TOKENS: Max tokens for generation (default: 8192)
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

        # Determine torch dtype
        torch_dtype_str = os.getenv("TORCH_DTYPE", "bfloat16")
        dtype_map = {
            "float16": torch.float16,
            "float32": torch.float32,
            "bfloat16": torch.bfloat16,
        }
        self.torch_dtype = dtype_map.get(torch_dtype_str, torch.bfloat16)

        # Model components (initialized in load_model)
        self.model = None
        self.processor = None
        self._is_loaded = False
        self._vibevoice_repo_path: Optional[Path] = None

        logger.info(
            f"VibeVoiceTranscriber initialized: "
            f"model={self.model_id}, llm={self.llm_model}, "
            f"device={self.device}, dtype={torch_dtype_str}, attn={self.attn_impl}"
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

        # Load model
        logger.info(f"Loading model with attn_implementation={self.attn_impl}")
        self.model = VibeVoiceASRForConditionalGeneration.from_pretrained(
            self.model_id,
            torch_dtype=self.torch_dtype,
            device_map="auto" if self.device == "cuda" else None,
            attn_implementation=self.attn_impl,
            trust_remote_code=True,
        )

        # Move to device (only needed if not using device_map)
        if self.device != "cuda":
            self.model = self.model.to(self.device)
            logger.info(f"Model moved to {self.device}")

        self.model.eval()

        self._is_loaded = True
        logger.info("VibeVoice model loaded successfully")

    def transcribe(self, audio_file_path: str) -> TranscriptionResult:
        """
        Transcribe audio file using VibeVoice with speaker diarization.

        Args:
            audio_file_path: Path to audio file

        Returns:
            TranscriptionResult with text, segments (with speakers), and speaker list
        """
        if not self._is_loaded or self.model is None or self.processor is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        logger.info(f"Transcribing: {audio_file_path}")

        # Process audio through processor (can take file paths directly)
        inputs = self.processor(
            audio=[audio_file_path],
            sampling_rate=None,
            return_tensors="pt",
            padding=True,
            add_generation_prompt=True,
        )

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
