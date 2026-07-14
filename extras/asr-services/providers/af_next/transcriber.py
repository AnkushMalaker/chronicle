"""
Audio Flamingo Next Transcriber.

Uses NVIDIA's Audio Flamingo Next (Think variant by default) for
prompt-driven speech transcription with timestamped multi-talker
diarization. The Think variant emits ``<think>...</think>`` reasoning
traces before its final answer; we strip those from the user-facing
transcript but stash them in ``TranscriptionResult.metadata`` and log
the full raw output so the reasoning is never silently dropped.

License: NVIDIA OneWay Noncommercial — research use only.

Audio expectations: mono, 16 kHz. The model windows long inputs
internally up to 30 minutes; we still batch ourselves above
``BATCH_THRESHOLD_SECONDS`` so progress can be reported.
"""

import logging
import os
import re
import wave
from typing import Optional

import torch
from common.batching import split_audio_file, stitch_transcription_results
from common.response_models import Segment, Speaker, TranscriptionResult
from transformers import AutoModel, AutoProcessor

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "nvidia/audio-flamingo-next-hf"
MAX_CHUNK_SECONDS = 1800  # AF-Next supports up to 30 minutes natively
# Window long audio into 2-min chunks. AF-Next doesn't just "summarise near the
# end" of a long window — on a long window it also *skips* low-priority opening
# material (rapid intro montage over music) entirely, transcribing only the
# "main" content. A 2-min clip it transcribes verbatim start-to-finish; a 5-/10-
# min clip drops the cold open and is erratic on word count. So keep windows
# small. Cost: ~22 windows for a 44-min episode, ~30 s each → ~12 min total.
BATCH_THRESHOLD_SECONDS = 120  # Batch into windows above this duration
BATCH_DURATION_SECONDS = 100  # Stride between windows when batching
BATCH_OVERLAP_SECONDS = 20  # → each window is 120s (the proven ceiling)

# Prompt (verified on CoSHE Hinglish + Shark Tank audio, May 2026):
# AF-Next is an audio-language *assistant*, not a dedicated ASR model — its chat
# template's baked-in system prompt tells it to "interpret the entirety of the
# content of any input audio". NVIDIA's model card guidance is therefore to ASK
# DIRECTLY for ASR; the documented ASR prompt is literally "Transcribe the input
# speech." (see https://huggingface.co/nvidia/audio-flamingo-next-hf, Prompt Guide).
#
# An elaborate "verbatim transcript ... begin from the first sound ... mark
# [music]/[applause] ... speaker labels ... no timestamps" prompt does the OPPOSITE
# of asking directly: every clause points the model at the non-speech scene and a
# timestamped template, so it slips into captioner mode — e.g.
#   "[0.0-35] A man speaks continuously. Music plays softly... silence"
# — instead of transcribing. On a worst-case CoSHE sample the elaborate prompt sat
# at ~100% median WER (pure descriptions); the bare prompt below dropped that to
# ~74% and turned descriptions back into real transcripts.
#
# Keep the task request BARE. Steer language / script / "do not translate" via
# context_info (passed per-request), NOT by enriching this prompt — adding language
# rules here re-triggers the description/template behavior. For multi-talker
# diarization, NVIDIA's documented prompt is: "Transcribe the input audio. If
# multiple speakers are present, provide diarized transcripts with speaker labels."
# (override via TRANSCRIPTION_PROMPT) — but it scores worse than the bare ASR prompt
# on hard single-channel Hinglish, so the bare prompt is the default.
DEFAULT_TRANSCRIPTION_PROMPT = "Transcribe the input speech."

# Speaker label at the START of a paragraph. Matches both "[Speaker N]"
# (Instruct default) and "Speaker N:" (when the model drops brackets).
# Optional leading "[start-end] " timestamp prefix is captured if present.
SPEAKER_PREFIX_RE = re.compile(
    r"^\s*"
    r"(?:\[(?P<start>\d+(?:\.\d+)?)\s*-\s*(?P<end>\d+(?:\.\d+)?)\]\s*)?"
    r"(?:\[(?P<bspeaker>Speaker\s*\d+)\]|(?P<pspeaker>Speaker\s*\d+):)\s*",
)
PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")
THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _strip_think(raw: str) -> tuple[str, str]:
    """Split raw model output into (answer, reasoning).

    AF-Next-Think emits ``<think>...</think>`` before the answer.
    Returns the trailing answer text and the concatenated reasoning
    content (empty string when no think block is present).
    """
    reasoning_parts = THINK_BLOCK_RE.findall(raw)
    answer = THINK_BLOCK_RE.sub("", raw).strip()
    reasoning = "\n\n".join(part.strip() for part in reasoning_parts).strip()
    return answer, reasoning


def _parse_diarized_text(
    text: str, chunk_start: float, chunk_end: float
) -> list[Segment]:
    """Parse AF-Next answer text into Segment objects.

    AF-Next-Instruct returns multi-line speaker blocks separated by blank
    lines. A paragraph either starts with a speaker label (`[Speaker N]` /
    `Speaker N:`) or is *unlabeled* — in which case we set ``speaker=None``
    rather than guessing, so the downstream speaker-recognition service
    can attribute it. Existing convention: integer/None speaker IDs are
    normalised to "Speaker N"/"unknown" later by the backend, and bracketed
    text like ``[music]`` is reclassified as ``segment_type="event"`` by
    ``utils.segment_utils.classify_segment_text``.

    Per-segment timestamps are honoured when the model emits them; otherwise
    we don't fake timing or diarization — the whole chunk is returned as one
    unknown-speaker segment.
    """
    clean = text.strip()
    if not clean or clean == "[NO SPEECH]":
        return []

    paragraphs = [p.strip() for p in PARAGRAPH_SPLIT_RE.split(text) if p.strip()]
    if not paragraphs:
        return []

    # Each item: (speaker, opt_start_str, opt_end_str, body)
    raw_segments: list[tuple[str | None, str | None, str | None, str]] = []
    for para in paragraphs:
        m = SPEAKER_PREFIX_RE.match(para)
        if m:
            speaker_raw = m.group("bspeaker") or m.group("pspeaker")
            speaker = speaker_raw.replace("  ", " ").strip() if speaker_raw else None
            body = para[m.end() :].strip(" \n\t:")
            ts_start = m.group("start")
            ts_end = m.group("end")
        else:
            speaker = None  # unlabeled — let speaker recognition re-attribute
            body = para.strip(" \n\t:")
            ts_start = ts_end = None
        if body:
            raw_segments.append((speaker, ts_start, ts_end, body))

    if not raw_segments:
        return [Segment(text=clean, start=chunk_start, end=chunk_end, speaker=None)]

    has_timestamps = all(s[1] is not None for s in raw_segments)
    segments: list[Segment] = []

    if has_timestamps:
        for speaker, ts_start, ts_end, body in raw_segments:
            if ts_start is None or ts_end is None:
                continue
            try:
                s = max(chunk_start, min(chunk_start + float(ts_start), chunk_end))
                e = max(s, min(chunk_start + float(ts_end), chunk_end))
            except (TypeError, ValueError):
                continue
            segments.append(
                Segment(text=body, start=round(s, 3), end=round(e, 3), speaker=speaker)
            )
        if segments:
            return segments

    # No real per-segment timestamps from the model — don't fake timing or
    # diarization. Collapse to ONE segment spanning the whole chunk with an
    # unknown speaker.
    clean = " ".join(body for _, _, _, body in raw_segments).strip()
    if not clean:
        return []
    return [Segment(text=clean, start=chunk_start, end=chunk_end, speaker=None)]


class AudioFlamingoNextTranscriber:
    """Transcriber using NVIDIA Audio Flamingo Next (Think variant)."""

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or os.getenv("ASR_MODEL", DEFAULT_MODEL)
        self.device = os.getenv(
            "DEVICE", "cuda" if torch.cuda.is_available() else "cpu"
        )
        self.torch_dtype = os.getenv("TORCH_DTYPE", "bfloat16")
        # Think traces are verbose; gemma4 uses 512, AF-Next-Think docs suggest 4096+
        self.max_new_tokens = int(os.getenv("MAX_NEW_TOKENS", "4096"))
        # AF-Next greedy decoding is prone to degenerate loops; bump higher if the
        # transcript still repeats. >~1.6 starts to distort legitimate repeats.
        self.repetition_penalty = float(os.getenv("REPETITION_PENALTY", "1.5"))
        self.batch_threshold = float(
            os.getenv("BATCH_THRESHOLD_SECONDS", str(BATCH_THRESHOLD_SECONDS))
        )
        self.batch_duration = float(
            os.getenv("BATCH_DURATION_SECONDS", str(BATCH_DURATION_SECONDS))
        )
        self.batch_overlap = float(
            os.getenv("BATCH_OVERLAP_SECONDS", str(BATCH_OVERLAP_SECONDS))
        )
        self.prompt = os.getenv("TRANSCRIPTION_PROMPT", DEFAULT_TRANSCRIPTION_PROMPT)
        self.processor = None
        self.model = None

    def load_model(self) -> None:
        """Load model and processor."""
        logger.info(f"Loading Audio Flamingo Next model: {self.model_id}")

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        dtype = dtype_map.get(self.torch_dtype, torch.bfloat16)

        self.processor = AutoProcessor.from_pretrained(self.model_id)
        self.model = AutoModel.from_pretrained(
            self.model_id,
            torch_dtype=dtype,
            device_map="auto",
        ).eval()
        logger.info(f"Model loaded on {self.device}")

    def _transcribe_single(
        self,
        audio_file_path: str,
        context_info: str | None = None,
        prompt_override: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a single audio file or window."""
        if self.processor is None or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        with wave.open(audio_file_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()

        if duration < 1.0:
            return TranscriptionResult(text="", segments=[], duration=duration)

        prompt = prompt_override or self.prompt
        if context_info:
            prompt += f"\n\nAdditional context / hot words: {context_info}"

        # AF-Next message format: text first, then audio path-typed content.
        conversation = [
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "audio", "path": audio_file_path},
                    ],
                }
            ]
        ]

        batch = self.processor.apply_chat_template(
            conversation,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
        ).to(self.model.device)

        # Audio features arrive as fp32; cast to model dtype before forward
        if "input_features" in batch:
            batch["input_features"] = batch["input_features"].to(self.model.dtype)

        input_len = batch["input_ids"].shape[-1]

        with torch.inference_mode():
            outputs = self.model.generate(
                **batch,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
                repetition_penalty=self.repetition_penalty,
                # AF-Next greedy decoding is prone to degenerate loops
                # ("[t-t] Ohh." x N, "haan haan haan ...") on Hinglish + music.
                # Block any repeated 4-gram to break those without hurting
                # legitimate short repetitions.
                no_repeat_ngram_size=4,
            )

        raw_text = self.processor.batch_decode(
            outputs[:, input_len:], skip_special_tokens=True
        )[0].strip()

        # Log the FULL output (no truncation) so the reasoning trace is
        # always recoverable from container logs.
        logger.info(
            f"AF-Next raw output ({duration:.1f}s, {len(raw_text)} chars):\n{raw_text}"
        )

        answer, reasoning = _strip_think(raw_text)
        segments = _parse_diarized_text(answer, 0.0, duration)

        speaker_ids = sorted({s.speaker for s in segments if s.speaker})
        speakers = (
            [
                Speaker(id=sid, label=None, start=0.0, end=duration)
                for sid in speaker_ids
            ]
            if speaker_ids
            else None
        )

        text = " ".join(s.text for s in segments) or answer

        metadata = {"raw_output": raw_text}
        if reasoning:
            metadata["reasoning"] = reasoning

        return TranscriptionResult(
            text=text,
            segments=segments,
            speakers=speakers,
            duration=duration,
            metadata=metadata,
        )

    def transcribe(
        self,
        audio_file_path: str,
        context_info: str | None = None,
        prompt_override: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio, batching long inputs."""
        with wave.open(audio_file_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()

        if duration <= self.batch_threshold:
            return self._transcribe_single(
                audio_file_path, context_info, prompt_override
            )

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
                f"Transcribing batch {i + 1}/{len(windows)} "
                f"[{start_time:.1f}s - {end_time:.1f}s]"
            )
            result = self._transcribe_single(window_path, context_info, prompt_override)
            batch_results.append((result, start_time, end_time))
            try:
                os.unlink(window_path)
            except OSError:
                pass

        return stitch_transcription_results(batch_results, self.batch_overlap)

    def supports_batch_progress(self, audio_duration: float) -> bool:
        return audio_duration > self.batch_threshold
