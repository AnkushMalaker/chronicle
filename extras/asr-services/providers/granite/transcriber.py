"""
IBM Granite Speech Transcriber.

Uses IBM's Granite Speech multimodal model (a Granite LLM backbone + a
conformer speech encoder fused with a built-in LoRA adapter) for speech-to-text.
Default target is ``ibm-granite/granite-speech-4.1-2b-plus`` (~2B params), which
runs comfortably in bf16 on a single GPU.

Like Gemma 4 / VibeVoice it is an LLM-backbone ASR: the audio is referenced by a
``<|audio|>`` placeholder inside a chat prompt, the prompt is rendered with the
tokenizer's chat template, and the processor splices the audio features in. It is
therefore prompt-conditioned, so a silence gate is used to stop it hallucinating
its prompt back on near-silent windows (see ``common.audio_utils.is_silent``).

Diarization (``GRANITE_DIARIZE``, default on) uses Granite for what it's good at —
transcription + speaker attribution (the model-card ``SAA_PROMPT``, which tags
``[Speaker N]`` turns) — and gets the TIMING from **forced alignment** (the
WhisperX pattern): the speaker-tagged transcript is aligned against the audio with
torchaudio's MMS_FA CTC model, yielding real per-word start/end times. Consecutive
same-speaker words are then grouped into segments. Granite's own ``[T:N]``
timestamp tags are NOT used — they saturate / under-time the tail of longer
windows — so segment times here come from a dedicated acoustic aligner, never from
guessing.
"""

import logging
import os
import re
import wave

import torch
from common.audio_utils import STANDARD_SAMPLE_RATE, is_silent, load_audio_file
from common.batching import split_audio_file, stitch_transcription_results
from common.forced_align import ForcedAligner
from common.response_models import Segment, Speaker, TranscriptionResult
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "ibm-granite/granite-speech-4.1-2b-plus"
MAX_CHUNK_SECONDS = 30
BATCH_THRESHOLD_SECONDS = 30
BATCH_OVERLAP_SECONDS = 5

# The placeholder the Granite processor swaps for the encoded audio features. It
# must appear in the user turn; the processor errors if it is missing.
AUDIO_TOKEN = "<|audio|>"

# IBM's recommended Granite chat system prompt (from the model card). The dates
# are part of IBM's canonical prompt and are left verbatim for fidelity.
SYSTEM_PROMPT = (
    "Knowledge Cutoff Date: April 2024.\n"
    "Today's Date: December 19, 2024.\n"
    "You are Granite, developed by IBM. You are a helpful AI assistant"
)

# IBM's recommended ASR instruction (the audio token is prepended separately).
DEFAULT_TRANSCRIPTION_PROMPT = "can you transcribe the speech into a written format?"

# IBM's recommended speaker-attribution prompt (model card, verbatim).
SAA_PROMPT = (
    "Speaker attribution: Transcribe and denote who is speaking by adding "
    "[Speaker 1]: and [Speaker 2]: tags before speaker turns."
)

# Silence gate — Granite Speech, like Gemma 4 / VibeVoice, is prompt-conditioned
# and echoes its prompt/context back as a phantom transcript on near-silent
# windows. Windows with < SILENCE_MIN_VOICED_MS of voiced audio (frame RMS >
# SILENCE_ENERGY_FLOOR on a [-1, 1] signal) skip the model and emit nothing.
SILENCE_ENERGY_FLOOR = 0.01
SILENCE_MIN_VOICED_MS = 200.0

_SAA_TAG_RE = re.compile(r"\[Speaker\s*(\d+)\]\s*:?\s*")


def _parse_saa_turns(text: str) -> list[tuple[str | None, str]]:
    """Parse a speaker-attribution transcript into ordered (speaker, turn_text).

    Returns ``[(None, text)]`` when no ``[Speaker N]`` tags are present.
    """
    matches = list(_SAA_TAG_RE.finditer(text))
    if not matches:
        clean = text.strip()
        return [(None, clean)] if clean else []
    turns: list[tuple[str | None, str]] = []
    for idx, m in enumerate(matches):
        speaker = f"Speaker {m.group(1)}"
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        turn_text = text[start:end].strip()
        if turn_text:
            turns.append((speaker, turn_text))
    return turns


def _segments_from_alignment(
    words: list[str],
    speakers: list[str | None],
    times: list[tuple[float, float] | None],
    duration: float,
) -> list[Segment]:
    """Group word-aligned, speaker-labelled words into contiguous segments.

    ``times[i]`` is the forced-aligned (start, end) of ``words[i]`` (or None when
    the word couldn't be tokenized for alignment). A segment spans a run of
    consecutive same-speaker words; its start/end come from the real alignment
    times of the words it contains. Unaligned words keep their text but contribute
    no timing.
    """
    raw: list[tuple[str | None, str, list[tuple[float, float]]]] = []
    cur_spk = speakers[0]
    cur_words: list[str] = []
    cur_times: list[tuple[float, float]] = []
    for w, spk, t in zip(words, speakers, times):
        if spk != cur_spk:
            raw.append((cur_spk, " ".join(cur_words), cur_times))
            cur_spk, cur_words, cur_times = spk, [], []
        cur_words.append(w)
        if t is not None:
            cur_times.append(t)
    raw.append((cur_spk, " ".join(cur_words), cur_times))

    segments: list[Segment] = []
    prev_end = 0.0
    for spk, text, ts in raw:
        if not text.strip():
            continue
        if ts:
            start = min(t[0] for t in ts)
            end = max(t[1] for t in ts)
        else:
            # No alignable word in this turn — bound it by the previous turn's
            # real end (a degenerate point; rare, e.g. an all-digit turn).
            start = end = prev_end
        start = max(0.0, start)
        end = min(max(end, start), duration)
        segments.append(Segment(text=text, start=start, end=end, speaker=spk))
        prev_end = end
    return segments


class GraniteTranscriber:
    """Transcriber using IBM Granite Speech multimodal model."""

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or os.getenv("ASR_MODEL", DEFAULT_MODEL)
        self.device = os.getenv(
            "DEVICE", "cuda" if torch.cuda.is_available() else "cpu"
        )
        self.torch_dtype = os.getenv("TORCH_DTYPE", "bfloat16")
        self.max_new_tokens = int(os.getenv("MAX_NEW_TOKENS", "512"))
        # Diarization on by default: one Granite speaker-attribution pass + forced
        # alignment for timing. The SAA pass is token-heavy (full transcript with
        # speaker tags), so it gets a larger budget.
        self.diarize = os.getenv("GRANITE_DIARIZE", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        self.diarize_max_new_tokens = int(
            os.getenv("GRANITE_DIARIZE_MAX_NEW_TOKENS", "1024")
        )
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
        self.processor = None
        self.tokenizer = None
        self.model = None
        self.aligner: ForcedAligner | None = None

    def load_model(self) -> None:
        """Load the processor + model (+ forced aligner when diarizing)."""
        logger.info(
            f"Loading Granite Speech model: {self.model_id} (diarize={self.diarize})"
        )

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        dtype = dtype_map.get(self.torch_dtype, torch.bfloat16)

        self.processor = AutoProcessor.from_pretrained(self.model_id)
        self.tokenizer = self.processor.tokenizer
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
            self.model_id, device_map=self.device, dtype=dtype
        )
        self.model.eval()

        if self.diarize:
            self.aligner = ForcedAligner(device=self.device)
            self.aligner.load()

        logger.info(f"Model loaded on {self.device}")

    def _load_waveform(self, audio_file_path: str) -> torch.Tensor:
        """Load audio as a (1, num_samples) 16 kHz mono float tensor.

        Uses the shared wave-based loader (mono float32 in [-1, 1], resampled to
        16 kHz) rather than ``torchaudio.load``, which in torchaudio 2.9 delegates
        to torchcodec (an extra native dependency we don't need).
        """
        audio_array, _ = load_audio_file(
            audio_file_path, target_rate=STANDARD_SAMPLE_RATE
        )
        return torch.from_numpy(audio_array).unsqueeze(0)

    def _build_prompt(self, prompt: str, context_info: str | None) -> str:
        """Assemble the user turn: audio token, instruction, optional context."""
        user_content = f"{AUDIO_TOKEN} {prompt}"
        if context_info:
            # Granite is an LLM, so naively appending context makes it transcribe
            # the context words themselves. Frame it as reference-only and forbid
            # echoing it. The backend already withholds the wake-word boost list
            # from context_prompt providers; this is defence in depth.
            user_content += (
                "\n\nReference context (background only — names, jargon, and "
                "spellings that may occur in the audio). Use it solely to "
                "disambiguate what is actually spoken. Never transcribe, repeat, or "
                "append this context; if none of it is spoken, ignore it entirely:\n"
                f"{context_info}"
            )
        return user_content

    def _generate(
        self, wav: torch.Tensor, user_content: str, max_new_tokens=None
    ) -> str:
        """Render the chat prompt, run generation, return the decoded new tokens."""
        chat = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        prompt_text = self.tokenizer.apply_chat_template(
            chat, tokenize=False, add_generation_prompt=True
        )
        inputs = self.processor(prompt_text, wav, return_tensors="pt").to(
            self.model.device
        )
        input_len = inputs["input_ids"].shape[-1]
        with torch.inference_mode():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens or self.max_new_tokens,
                do_sample=False,
                num_beams=1,
            )
        return self.tokenizer.decode(
            outputs[0][input_len:], skip_special_tokens=True
        ).strip()

    def _transcribe_single(
        self,
        audio_file_path: str,
        context_info: str | None = None,
        prompt_override: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a single audio chunk (must be <= 30s)."""
        if self.processor is None or self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        with wave.open(audio_file_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()

        # Skip very short chunks (likely silence/noise)
        if duration < 1.0:
            return TranscriptionResult(text="", segments=[], duration=duration)

        wav = self._load_waveform(audio_file_path)

        # Silence gate: skip near-silent windows. A prompt-conditioned ASR echoes
        # its prompt/context back as a phantom transcript on silence, so emit nothing.
        if self.silence_gate_enabled and is_silent(
            wav.squeeze(0).cpu().numpy(),
            STANDARD_SAMPLE_RATE,
            energy_floor=self.silence_energy_floor,
            min_voiced_ms=self.silence_min_voiced_ms,
        ):
            logger.info(
                f"Silence gate: {audio_file_path} has < {self.silence_min_voiced_ms:.0f}ms "
                f"voiced audio (floor={self.silence_energy_floor}); skipping ASR, emitting silence"
            )
            return TranscriptionResult(text="", segments=[], duration=duration)

        # Diarized path: Granite speaker-attribution pass + forced-alignment timing.
        # A prompt_override forces the plain single-pass path (raw ASR, caller prompt).
        if self.diarize and not prompt_override:
            return self._transcribe_diarized(wav, duration, context_info)

        prompt = prompt_override or self.prompt
        text = self._generate(wav, self._build_prompt(prompt, context_info))
        logger.info(f"Raw output ({duration:.1f}s): {text[:200]}")
        if not text:
            return TranscriptionResult(text="", segments=[], duration=duration)
        segment = Segment(text=text, start=0.0, end=duration, speaker=None)
        return TranscriptionResult(text=text, segments=[segment], duration=duration)

    def _transcribe_diarized(
        self, wav: torch.Tensor, duration: float, context_info: str | None
    ) -> TranscriptionResult:
        """Speaker attribution (Granite) + word timing (MMS_FA forced alignment)."""
        saa_text = self._generate(
            wav,
            self._build_prompt(SAA_PROMPT, context_info),
            max_new_tokens=self.diarize_max_new_tokens,
        )
        logger.info(f"SAA pass ({duration:.1f}s): {saa_text[:200]}")

        turns = _parse_saa_turns(saa_text)
        words: list[str] = []
        speakers: list[str | None] = []
        for speaker, turn_text in turns:
            for w in turn_text.split():
                words.append(w)
                speakers.append(speaker)

        if not words:
            return TranscriptionResult(text="", segments=[], duration=duration)

        # Real per-word timestamps from the acoustic aligner (not Granite's tags).
        times = self.aligner.align(wav, words)
        segments = _segments_from_alignment(words, speakers, times, duration)
        if not segments:
            return TranscriptionResult(text="", segments=[], duration=duration)

        text = " ".join(s.text for s in segments)
        speaker_ids = sorted({s.speaker for s in segments if s.speaker})
        speakers_out = (
            [
                Speaker(
                    id=sid,
                    label=None,
                    start=min(s.start for s in segments if s.speaker == sid),
                    end=max(s.end for s in segments if s.speaker == sid),
                )
                for sid in speaker_ids
            ]
            if speaker_ids
            else None
        )
        return TranscriptionResult(
            text=text, segments=segments, speakers=speakers_out, duration=duration
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
            try:
                os.unlink(window_path)
            except OSError:
                pass

            yield {"type": "progress", "current": i + 1, "total": len(windows)}

        yield {
            "type": "result",
            "result": stitch_transcription_results(batch_results, self.batch_overlap),
        }

    def supports_batch_progress(self, audio_duration: float) -> bool:
        return audio_duration > self.batch_threshold
