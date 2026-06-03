"""
Gemma 4 E4B-it Transcriber.

Uses Google's Gemma 4 multimodal model for speech-to-text with
prompt-based speaker diarization. Max audio input: 30 seconds per chunk.
"""

import logging
import os
import re
import tempfile
import wave

import torch
from common.batching import split_audio_file, stitch_transcription_results
from common.response_models import Segment, Speaker, TranscriptionResult
from transformers import AutoModelForMultimodalLM, AutoProcessor

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "google/gemma-4-E4B-it"
MAX_CHUNK_SECONDS = 30
BATCH_THRESHOLD_SECONDS = 30
BATCH_OVERLAP_SECONDS = 5

# Google's recommended sampling configuration for Gemma 4 (all use cases).
GEMMA4_TEMPERATURE = 1.0
GEMMA4_TOP_P = 0.95
GEMMA4_TOP_K = 64

DEFAULT_TRANSCRIPTION_PROMPT = (
    "Transcribe the following speech segment in its original language and identify different speakers. "
    "Follow these specific instructions for formatting the answer:\n"
    "* Label each speaker as Speaker 1, Speaker 2, etc.\n"
    "* Format each turn as 'Speaker N: <text>' on its own line.\n"
    "* Start a new line when the speaker changes.\n"
    "* When transcribing numbers, write the digits, i.e. write 1.7 and not "
    "one point seven, and write 3 instead of three.\n"
    "* If the audio is silence or contains no speech, respond with exactly: [NO SPEECH]"
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
    """Transcriber using Google Gemma 4 E4B-it multimodal model."""

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
        self.processor = None
        self.model = None

    def load_model(self) -> None:
        """Load model and processor."""
        logger.info(f"Loading Gemma 4 model: {self.model_id}")

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        dtype = dtype_map.get(self.torch_dtype, torch.bfloat16)

        self.processor = AutoProcessor.from_pretrained(self.model_id)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            self.model_id,
            dtype=dtype,
            device_map="auto",
        )
        logger.info(f"Model loaded on {self.device}")

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

        prompt = prompt_override or self.prompt
        if context_info:
            prompt += f"\n* Context/keywords: {context_info}"

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

        with torch.inference_mode():
            outputs = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens)

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

        # Batch mode: split into overlapping chunks
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

        return stitch_transcription_results(batch_results, self.batch_overlap)

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

        # Normalize plain-string content to structured format for multimodal processor
        normalized = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                normalized.append(
                    {
                        "role": msg["role"],
                        "content": [{"type": "text", "text": content}],
                    }
                )
            else:
                normalized.append(msg)

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

        with torch.inference_mode():
            outputs = self.model.generate(**inputs, **gen_kwargs)

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
        with torch.inference_mode():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=1024,
                do_sample=False,
            )

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
        import json as _json

        # Try direct JSON parse first
        # The model may wrap JSON in markdown code fences
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            # Strip markdown code fence
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        try:
            result = _json.loads(cleaned)
            # Validate expected fields
            return {
                "verdict": result.get("verdict", "inaccurate"),
                "confidence": float(result.get("confidence", 0.5)),
                "errors": result.get("errors", []),
                "reasoning": result.get("reasoning", ""),
            }
        except (_json.JSONDecodeError, ValueError):
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
