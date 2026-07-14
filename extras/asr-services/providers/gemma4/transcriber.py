"""
Gemma 4 E2B-it Transcriber.

Uses Google's Gemma 4 multimodal model for speech-to-text with prompt-based
speaker diarization. Default target is the E2B-it model (2.3B effective / ~5.1B
with embeddings, ~10GB in bf16) — small enough to run full precision on a 24GB
GPU, so the default ``GEMMA4_QUANT`` is bf16 (4bit/8bit remain available for
smaller GPUs; the audio/vision towers are kept out of any quantization).

Decoding is accelerated with Multi-Token Prediction (MTP): a small text-only
``-assistant`` drafter proposes tokens that the target verifies (``GEMMA4_MTP``).
Verified output is drawn from the target's own distribution, so MTP does not
change quality. Every Gemma 4 size ships a matching ``*-it-assistant`` drafter
(``gemma-4-E2B-it-assistant`` is 78M params).
"""

import base64
import json
import logging
import os
import re
import tempfile
import wave

import torch
from common.audio_utils import (
    STANDARD_SAMPLE_RATE,
    is_silent,
    load_audio_bytes,
    load_audio_file,
)
from common.batching import split_audio_file, stitch_transcription_results
from common.response_models import Segment, Speaker, TranscriptionResult
from transformers import (
    AutoModelForCausalLM,
    AutoModelForMultimodalLM,
    AutoProcessor,
    BitsAndBytesConfig,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "google/gemma-4-E2B-it"
MAX_CHUNK_SECONDS = 30
BATCH_THRESHOLD_SECONDS = 30
BATCH_OVERLAP_SECONDS = 5

# Silence gate — Gemma 4, like VibeVoice, is prompt-conditioned and echoes its
# prompt/context back as a phantom transcript on near-silent windows. Windows with
# < SILENCE_MIN_VOICED_MS of voiced audio (frame RMS > SILENCE_ENERGY_FLOOR on a
# [-1,1] signal) skip the model and emit nothing. Thresholds validated on real
# captures (echo windows 0-60ms voiced; real speech 510ms+).
SILENCE_ENERGY_FLOOR = 0.01
SILENCE_MIN_VOICED_MS = 200.0

# 4-bit/8-bit must never touch the audio/vision towers: their Gemma4ClippableLinear
# calls torch.finfo(weight.dtype), which breaks on uint8 quantized weights. The
# "model." prefix is required — should_convert_module matches start-anchored.
QUANT_SKIP_MODULES = [
    "model.audio_tower",
    "model.vision_tower",
    "model.embed_audio",
    "model.embed_vision",
    "lm_head",
]

# Google's recommended sampling configuration for Gemma 4 (all use cases).
GEMMA4_TEMPERATURE = 1.0
GEMMA4_TOP_P = 0.95
GEMMA4_TOP_K = 64

# Google's recommended ASR prompt for Gemma 4 (kept deliberately minimal). The
# elaborate "identify speakers / Speaker N:" framing was dropped: the diarized
# labels were discarded downstream anyway (_parse_diarized_text strips them and
# returns one speaker=None segment), and the instruction soup — plus the old
# "respond with [NO SPEECH]" escape hatch — pushed Gemma 4 into its reasoning
# channel and made it refuse / mis-fire [NO SPEECH] on real speech.
DEFAULT_TRANSCRIPTION_PROMPT = (
    "Transcribe the following speech segment in its original language into text. "
    "Only output the transcription text itself, with no commentary or explanation. "
    "When transcribing numbers, write the digits, i.e. write 1.7 and not "
    "one point seven, and write 3 instead of three."
)

# Regex to parse "Speaker N: text" lines
SPEAKER_LINE_RE = re.compile(r"^(Speaker \d+):\s*(.+)$", re.MULTILINE)

# Judge strictness instruction templates
_JUDGE_INSTRUCTIONS = {
    "strict": (
        "Be VERY strict — flag ANY discrepancy, no matter how minor.\n"
        "Check for:\n"
        "- Wrong words (even slight mishearings or spelling variants)\n"
        "- Missing words (words spoken but not in transcript)\n"
        "- Extra words (words in transcript but not spoken)\n"
        "- Wrong speaker attribution\n"
        "- Incorrect numbers or proper nouns\n"
        "- Missing filler words or interjections"
    ),
    "balanced": (
        "Focus on meaningful errors that change the meaning. "
        "Ignore minor formatting or punctuation differences.\n"
        "Check for:\n"
        "- Words that are clearly wrong (not just spelling variants)\n"
        "- Missing or hallucinated phrases\n"
        "- Speaker mix-ups (if speakers are labeled)\n"
        "- Incorrect numbers, names, or key terms"
    ),
    "lenient": (
        "Only flag MAJOR errors. Ignore minor word variations, "
        "filler words, or small omissions.\n"
        "Only flag if:\n"
        "- The transcript says something completely different from what was spoken\n"
        "- Large chunks of speech are missing or fabricated\n"
        "- Speaker labels are completely wrong\n"
        "- Key facts (numbers, names) are wrong"
    ),
}


def _normalize_chat_content(content: list) -> list:
    """Decode chat content audio parts into the array form the Gemma processor expects.

    Only OpenAI-style ``{"type":"input_audio","input_audio":{"data":<b64>,"format":"wav"}}``
    needs work (no file to hand the processor): decode the base64 ONCE to a numpy array via
    ``load_audio_bytes`` (no temp file). Everything else — text, already-decoded ndarray audio,
    and local path refs ``{"type":"audio","audio":"<path>"}`` — passes through untouched; the
    processor loads paths natively (its own loader), which keeps path-ref results identical to
    the /transcribe path and avoids any decode at all.
    """
    out = []
    for part in content:
        if part.get("type") == "input_audio":
            audio_bytes = base64.b64decode(part["input_audio"]["data"])
            array, _ = load_audio_bytes(audio_bytes)
            out.append({"type": "audio", "audio": array})
        else:
            out.append(part)
    return out


def _parse_diarized_text(
    text: str, chunk_start: float, chunk_end: float
) -> list[Segment]:
    """Parse Gemma 4 diarized output into a single segment.

    Gemma 4 emits speaker-labelled text but no real timestamps. We do NOT fake
    per-speaker timing or diarization: the speaker labels are stripped and the
    whole chunk is returned as ONE segment spanning [chunk_start, chunk_end] with
    an unknown speaker (chunk_start/chunk_end are the real audio boundaries).
    """
    matches = list(SPEAKER_LINE_RE.finditer(text))
    if matches:
        clean = " ".join(m.group(2).strip() for m in matches if m.group(2).strip())
    else:
        clean = text.strip()
    if not clean or clean == "[NO SPEECH]":
        return []
    return [Segment(text=clean, start=chunk_start, end=chunk_end, speaker=None)]


class Gemma4Transcriber:
    """Transcriber using Google Gemma 4 E2B-it multimodal model."""

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or os.getenv("ASR_MODEL", DEFAULT_MODEL)
        self.device = os.getenv(
            "DEVICE", "cuda" if torch.cuda.is_available() else "cpu"
        )
        self.torch_dtype = os.getenv("TORCH_DTYPE", "bfloat16")
        self.max_new_tokens = int(os.getenv("MAX_NEW_TOKENS", "512"))
        self.batch_threshold = float(
            os.getenv("BATCH_THRESHOLD_SECONDS", str(BATCH_THRESHOLD_SECONDS))
        )
        self.batch_duration = float(
            os.getenv("BATCH_DURATION_SECONDS", str(MAX_CHUNK_SECONDS))
        )
        self.batch_overlap = float(
            os.getenv("BATCH_OVERLAP_SECONDS", str(BATCH_OVERLAP_SECONDS))
        )
        self.prompt = os.getenv("TRANSCRIPTION_PROMPT", DEFAULT_TRANSCRIPTION_PROMPT)

        self.silence_gate_enabled = os.getenv(
            "SILENCE_GATE_ENABLED", "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        self.silence_energy_floor = float(
            os.getenv("SILENCE_ENERGY_FLOOR") or SILENCE_ENERGY_FLOOR
        )
        self.silence_min_voiced_ms = float(
            os.getenv("SILENCE_MIN_VOICED_MS") or SILENCE_MIN_VOICED_MS
        )
        # Quantization: bf16 (default — E2B is ~10GB, fits a 24GB GPU full
        # precision), 8bit, or 4bit (for smaller GPUs).
        self.quant = os.getenv("GEMMA4_QUANT", "bf16").lower()
        # MTP: text-only -assistant drafter for speculative decoding (~1.6x on ASR).
        self.mtp_enabled = os.getenv("GEMMA4_MTP", "1") == "1"
        self.assistant_model_id = (
            os.getenv("GEMMA4_ASSISTANT_MODEL") or f"{self.model_id}-assistant"
        )
        self.processor = None
        self.model = None
        self.assistant_model = None

    def load_model(self) -> None:
        """Load model, processor, and (optionally) the MTP assistant drafter."""
        logger.info(
            f"Loading Gemma 4 model: {self.model_id} "
            f"(quant={self.quant}, mtp={self.mtp_enabled})"
        )

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        dtype = dtype_map.get(self.torch_dtype, torch.bfloat16)

        load_kwargs: dict = {"dtype": dtype, "device_map": "auto"}
        if self.quant == "4bit":
            load_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=dtype,
                llm_int8_skip_modules=QUANT_SKIP_MODULES,
            )
        elif self.quant == "8bit":
            load_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_8bit=True,
                llm_int8_skip_modules=QUANT_SKIP_MODULES,
            )
        elif self.quant != "bf16":
            raise ValueError(
                f"Invalid GEMMA4_QUANT={self.quant!r}; expected 4bit, 8bit, or bf16"
            )

        self.processor = AutoProcessor.from_pretrained(self.model_id)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            self.model_id, **load_kwargs
        )
        self.model.eval()

        if self.mtp_enabled:
            logger.info(f"Loading MTP assistant drafter: {self.assistant_model_id}")
            # The drafter is small (~0.8GB) and text-only; load it in bf16 even when
            # the target is quantized (validated combination).
            self.assistant_model = AutoModelForCausalLM.from_pretrained(
                self.assistant_model_id, dtype=dtype, device_map="auto"
            )
            self.assistant_model.eval()

        logger.info(f"Model loaded on {self.device}")

    def _generate(self, inputs: dict, **gen_kwargs):
        """Run model.generate, injecting the MTP assistant when enabled."""
        if self.assistant_model is not None:
            gen_kwargs["assistant_model"] = self.assistant_model
        with torch.inference_mode():
            return self.model.generate(**inputs, **gen_kwargs)

    def _decode_response(self, outputs, input_len: int) -> str:
        """Decode generated tokens and strip any thinking/channel blocks.

        Uses the processor's ``parse_response`` (the Gemma 4 recommended flow) so
        that ``<|channel>thought ... <channel|>`` wrappers never leak into the
        returned text, regardless of whether thinking was triggered.

        ``parse_response`` returns a chat-message dict ``{"role": "assistant",
        "thinking"?: ..., "content": ..., "tool_calls"?: ...}`` per the Gemma 4
        ``response_schema``. We want ``content``; ``thinking`` is dropped.
        """
        raw = self.processor.decode(outputs[0][input_len:], skip_special_tokens=False)
        parsed = self.processor.parse_response(raw)
        if isinstance(parsed, dict):
            text = parsed.get("content") or ""
        else:
            text = parsed or ""
        return text.strip()

    def _transcribe_single(
        self,
        audio_file_path: str,
        context_info: str | None = None,
        prompt_override: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a single audio chunk (must be <= 30s)."""
        if self.processor is None or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        # Get duration for segment timing
        with wave.open(audio_file_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()

        # Skip very short chunks (likely silence/noise)
        if duration < 1.0:
            return TranscriptionResult(text="", segments=[], duration=duration)

        # Silence gate: skip near-silent windows. A prompt-conditioned ASR echoes
        # its prompt/context back as a phantom transcript on silence, so emit nothing.
        if self.silence_gate_enabled:
            audio_array, sr = load_audio_file(
                audio_file_path, target_rate=STANDARD_SAMPLE_RATE
            )
            if is_silent(
                audio_array,
                sr,
                energy_floor=self.silence_energy_floor,
                min_voiced_ms=self.silence_min_voiced_ms,
            ):
                logger.info(
                    f"Silence gate: {audio_file_path} has < {self.silence_min_voiced_ms:.0f}ms "
                    f"voiced audio (floor={self.silence_energy_floor}); skipping ASR, emitting silence"
                )
                return TranscriptionResult(text="", segments=[], duration=duration)

        prompt = prompt_override or self.prompt
        if context_info:
            # Gemma 4 is an LLM, so naively appending the context made it transcribe
            # the context words themselves (e.g. injected wake words leaked into the
            # output). Frame the context as reference-only and forbid echoing it.
            # The backend already withholds the wake-word boost list from
            # context_prompt providers; this is defence in depth.
            prompt += (
                "\n\nReference context (background only — names, jargon, and "
                "spellings that may occur in the audio). Use it solely to "
                "disambiguate what is actually spoken. Never transcribe, repeat, or "
                "append this context; if none of it is spoken, ignore it entirely:\n"
                f"{context_info}"
            )

        logger.info(f"Using prompt: {prompt[:100]}...")

        # Gemma 4 guidance: place audio AFTER the text for best multimodal
        # performance. The prompt text ("the following speech segment") also
        # reads naturally with the audio coming after it.
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "audio", "audio": audio_file_path},
                ],
            }
        ]

        inputs = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            add_generation_prompt=True,
            enable_thinking=False,
        ).to(self.model.device)

        input_len = inputs["input_ids"].shape[-1]

        outputs = self._generate(inputs, max_new_tokens=self.max_new_tokens)

        raw_text = self._decode_response(outputs, input_len)
        logger.info(f"Raw output ({duration:.1f}s): {raw_text[:200]}")

        # Parse into segments
        segments = _parse_diarized_text(raw_text, 0.0, duration)

        # Build speaker list
        speaker_ids = sorted({s.speaker for s in segments if s.speaker})
        speakers = (
            [
                Speaker(id=sid, label=None, start=0.0, end=duration)
                for sid in speaker_ids
            ]
            if speaker_ids
            else None
        )

        # Build plain text
        text = " ".join(s.text for s in segments)

        return TranscriptionResult(
            text=text,
            segments=segments,
            speakers=speakers,
            duration=duration,
        )

    def transcribe(
        self,
        audio_file_path: str,
        context_info: str | None = None,
        prompt_override: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio file, batching if longer than threshold."""
        with wave.open(audio_file_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()

        if duration <= self.batch_threshold:
            return self._transcribe_single(
                audio_file_path, context_info, prompt_override
            )

        # Batch mode: drain the shared progress generator and return its final
        # result. The generator is also exposed (via the service layer) for
        # NDJSON progress streaming on long audio.
        result: TranscriptionResult | None = None
        for event in self._transcribe_batched_with_progress(
            audio_file_path,
            context_info=context_info,
            prompt_override=prompt_override,
        ):
            if event["type"] == "result":
                result = event["result"]
        assert result is not None, "batched transcription yielded no result event"
        return result

    def _transcribe_batched_with_progress(
        self,
        audio_file_path: str,
        context_info: str | None = None,
        prompt_override: str | None = None,
    ):
        """Transcribe long audio in overlapping windows, reporting progress.

        Splits the audio into windows, transcribes each, and stitches the
        results. A progress event is yielded after every window so the HTTP
        client keeps receiving bytes during a multi-minute transcription
        (preventing read timeouts on long audio); the final result is yielded
        last as a ``TranscriptionResult`` object.

        Yields:
            {"type": "progress", "current": i, "total": n} after each window
            {"type": "result", "result": TranscriptionResult} as the final item
        """
        with wave.open(audio_file_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()

        logger.info(
            f"Audio is {duration:.1f}s (>{self.batch_threshold}s), "
            f"batching with {self.batch_duration}s windows, {self.batch_overlap}s overlap"
        )
        windows = split_audio_file(
            audio_file_path,
            batch_duration=self.batch_duration,
            overlap=self.batch_overlap,
        )

        batch_results = []
        for i, (window_path, start_time, end_time) in enumerate(windows):
            logger.info(
                f"Transcribing batch {i + 1}/{len(windows)} [{start_time:.1f}s - {end_time:.1f}s]"
            )
            result = self._transcribe_single(window_path, context_info, prompt_override)
            batch_results.append((result, start_time, end_time))
            # Clean up temp window file
            try:
                os.unlink(window_path)
            except OSError:
                pass

            yield {"type": "progress", "current": i + 1, "total": len(windows)}

        yield {
            "type": "result",
            "result": stitch_transcription_results(batch_results, self.batch_overlap),
        }

    def generate_chat(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        temperature: float = GEMMA4_TEMPERATURE,
        top_p: float = GEMMA4_TOP_P,
        top_k: int = GEMMA4_TOP_K,
    ) -> tuple[str, int, int]:
        """Generate a text-only chat completion reusing the loaded model.

        Args:
            messages: OpenAI-format messages list (text-only, no audio).
            max_tokens: Maximum tokens to generate (default 2000).
            temperature: Sampling temperature. <=0 uses greedy decoding.
            top_p: Nucleus sampling cutoff (only used when sampling).
            top_k: Top-k sampling cutoff (only used when sampling).

        Returns:
            (generated_text, prompt_tokens, completion_tokens)
        """
        if self.processor is None or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        effective_max_tokens = max_tokens or 2000

        # Normalize content to the structured form the multimodal processor expects:
        # plain strings -> a single text part; lists may carry audio parts (base64
        # input_audio or path refs) which _normalize_chat_content decodes to arrays.
        normalized = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            else:
                content = _normalize_chat_content(content)
            normalized.append({"role": msg["role"], "content": content})

        inputs = self.processor.apply_chat_template(
            normalized,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            add_generation_prompt=True,
            enable_thinking=False,
        ).to(self.model.device)

        input_len = inputs["input_ids"].shape[-1]

        gen_kwargs: dict = {"max_new_tokens": effective_max_tokens}
        if temperature > 0:
            gen_kwargs["do_sample"] = True
            gen_kwargs["temperature"] = temperature
            gen_kwargs["top_p"] = top_p
            gen_kwargs["top_k"] = top_k
        else:
            gen_kwargs["do_sample"] = False

        outputs = self._generate(inputs, **gen_kwargs)

        completion_tokens = outputs.shape[-1] - input_len
        text = self._decode_response(outputs, input_len)

        return text, int(input_len), int(completion_tokens)

    def judge_single(
        self,
        audio_file_path: str,
        transcript: str,
        context: str = "",
        strictness: str = "balanced",
    ) -> dict:
        """Judge whether a transcript segment is accurate for an audio clip.

        Uses the multimodal model to listen to the audio and compare against
        the provided transcript, with surrounding transcript context for
        cross-referencing recurring words/names/entities.

        Args:
            audio_file_path: Path to a short audio clip (ideally ~10s)
            transcript: The transcript text to verify for this clip
            context: Surrounding transcript text (before + after) for cross-reference
            strictness: One of "strict", "balanced", "lenient"

        Returns:
            Dict with verdict, confidence, errors list, and reasoning
        """
        if self.processor is None or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        prompt = self._build_judge_prompt(transcript, context, strictness)

        # Gemma 4 guidance: audio goes AFTER the text. The judge prompt also
        # refers to "the audio clip below", so the audio must follow the text.
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "audio", "audio": audio_file_path},
                ],
            }
        ]

        inputs = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            add_generation_prompt=True,
            enable_thinking=False,
        ).to(self.model.device)

        input_len = inputs["input_ids"].shape[-1]

        # Greedy decoding here is deliberate: the judge emits a structured JSON
        # verdict and we want it deterministic across runs.
        outputs = self._generate(inputs, max_new_tokens=1024, do_sample=False)

        raw_text = self._decode_response(outputs, input_len)
        logger.info(f"Judge raw output: {raw_text[:300]}")

        return self._parse_judge_output(raw_text)

    def _build_judge_prompt(
        self, transcript: str, context: str, strictness: str
    ) -> str:
        """Build the judge prompt based on strictness level."""
        context_block = ""
        if context:
            context_block = (
                "SURROUNDING TRANSCRIPT (for reference — use this to cross-reference "
                "recurring words, names, and terms):\n"
                f"{context}\n\n"
            )

        strictness_instructions = _JUDGE_INSTRUCTIONS.get(
            strictness, _JUDGE_INSTRUCTIONS["balanced"]
        )

        return (
            "You are an audio transcription quality auditor. "
            "Listen to the audio clip below and evaluate whether the transcript is accurate.\n\n"
            f"{context_block}"
            "TRANSCRIPT TO VERIFY (for the audio clip below):\n"
            f"{transcript}\n\n"
            f"{strictness_instructions}\n\n"
            "IMPORTANT: Use the surrounding transcript to cross-reference. "
            "If a word appears differently here than it consistently appears "
            "elsewhere in the transcript, flag it as likely wrong.\n\n"
            "Respond with ONLY a JSON object in this exact format:\n"
            '{"verdict": "accurate" or "inaccurate", "confidence": 0.0 to 1.0, '
            '"errors": [{"type": "wrong_word|missing_word|extra_word|speaker_error", '
            '"detail": "description"}], "reasoning": "your step-by-step analysis"}'
        )

    @staticmethod
    def _parse_judge_output(raw_text: str) -> dict:
        """Parse the model's judge output into a structured dict."""
        # Try direct JSON parse first
        # The model may wrap JSON in markdown code fences
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            # Strip markdown code fence
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        try:
            result = json.loads(cleaned)
            # Validate expected fields
            return {
                "verdict": result.get("verdict", "inaccurate"),
                "confidence": float(result.get("confidence", 0.5)),
                "errors": result.get("errors", []),
                "reasoning": result.get("reasoning", ""),
            }
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: regex extraction
        verdict = "inaccurate"
        if re.search(r'"verdict"\s*:\s*"accurate"', raw_text):
            verdict = "accurate"

        confidence = 0.5
        conf_match = re.search(r'"confidence"\s*:\s*([\d.]+)', raw_text)
        if conf_match:
            try:
                confidence = float(conf_match.group(1))
            except ValueError:
                pass

        reasoning_match = re.search(r'"reasoning"\s*:\s*"([^"]*)"', raw_text)
        reasoning = reasoning_match.group(1) if reasoning_match else raw_text[:500]

        return {
            "verdict": verdict,
            "confidence": confidence,
            "errors": [],
            "reasoning": reasoning,
        }

    def supports_batch_progress(self, audio_duration: float) -> bool:
        return audio_duration > self.batch_threshold
