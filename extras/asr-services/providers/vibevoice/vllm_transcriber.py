"""VibeVoice ASR via vLLM — single-pass, no windowing.

Proxies to a local vLLM OpenAI server running the microsoft VibeVoice ``vllm_plugin``
(the model-supported inference path). Unlike the transformers path
(``transcriber.py``), this does NOT window the audio: VibeVoice's designed capacity
is 61 min in a single pass, and full-context single-pass is both faster
(vLLM paged-attention) and far more robust — isolating a short window with no
surrounding context is what tips the decoder into degeneration (repetition /
JSON-overflow collapse). Benchmark + rationale: memory
``vibevoice-vllm-vs-transformers-bench``.

The catastrophic failure mode (JSON overflow → template leak) does not occur in
single-pass full context, but the model still emits localized repetition loops on
hard far-field/noisy speech, so the same post-decode ``_collapse_loops`` hardening
as the transformers path is applied here and surfaced on the System Errors page.
"""

import base64
import json
import logging
import os
import re
import threading
import wave
from typing import Optional

import requests
from common.response_models import Segment, Speaker, TranscriptionResult

logger = logging.getLogger(__name__)

# Raw chat-template tokens leaking into output == decoder degeneration, never speech.
_TEMPLATE_MARKERS = ("<|im_start|>", "<|im_end|>", "<|endoftext|>")
# The four fields the VibeVoice vLLM plugin is prompted to emit per segment.
_KEYS = ["Start time", "End time", "Speaker ID", "Content"]
_SYSTEM_PROMPT = (
    "You are a helpful assistant that transcribes audio input into text "
    "output in JSON format."
)


def _audio_duration(path: str) -> float:
    """Duration in seconds via stdlib wave (audio arrives as PCM16 WAV)."""
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / wf.getframerate()


class VibeVoiceVllmTranscriber:
    """Single-pass VibeVoice transcription against a local vLLM OpenAI server.

    Environment variables:
        VIBEVOICE_VLLM_URL: vLLM base URL (default http://127.0.0.1:18000)
        VIBEVOICE_VLLM_SERVED_NAME: served-model-name (default "vibevoice")
        VIBEVOICE_VLLM_MAX_TOKENS: generation cap (default 16384). Bounds a
            degenerate request so it can't run away to the model max.
        VIBEVOICE_VLLM_TIMEOUT: per-request HTTP timeout secs (default 1800)
        VIBEVOICE_PROGRESS_HEARTBEAT: secs between NDJSON heartbeats (default 2)
        BATCH_THRESHOLD_SECONDS: stream progress for audio longer than this so the
            backend's HTTP read doesn't time out during a multi-minute pass (default 60)
    """

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (
            base_url or os.getenv("VIBEVOICE_VLLM_URL", "http://127.0.0.1:18000")
        ).rstrip("/")
        self.model_name = os.getenv("VIBEVOICE_VLLM_SERVED_NAME", "vibevoice")
        self.max_tokens = int(os.getenv("VIBEVOICE_VLLM_MAX_TOKENS", "16384"))
        self.request_timeout = float(os.getenv("VIBEVOICE_VLLM_TIMEOUT", "1800"))
        self.heartbeat_secs = float(os.getenv("VIBEVOICE_PROGRESS_HEARTBEAT", "2"))
        self.progress_threshold = float(os.getenv("BATCH_THRESHOLD_SECONDS", "60"))
        self._is_loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    def supports_batch_progress(self, audio_duration: float) -> bool:
        """Long audio streams NDJSON heartbeats so the backend read doesn't time out."""
        return audio_duration > self.progress_threshold

    # ------------------------------------------------------------------ hardening
    @staticmethod
    def _collapse_loops(text: str, keep: int = 2) -> tuple[str, int]:
        """Collapse contiguous same-token runs to <= keep copies (lossless for
        natural doubles). Returns (collapsed_text, longest_run). Ported verbatim
        from the transformers path so both backends harden identically."""
        if not text:
            return text, 0
        tokens = text.split()
        out: list[str] = []
        longest = 0
        i = 0
        while i < len(tokens):
            key = tokens[i].lower().strip(".,!?;:")
            j = i
            while j < len(tokens) and tokens[j].lower().strip(".,!?;:") == key:
                j += 1
            longest = max(longest, j - i)
            out.extend(tokens[i : i + min(j - i, keep)])
            i = j
        return " ".join(out), longest

    _LOOP_REPORT_RUN = 8

    # ------------------------------------------------------------------ requests
    def _build_payload(
        self,
        audio_path: str,
        duration: float,
        context_info: Optional[str],
        stream: bool,
    ) -> dict:
        with open(audio_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        if context_info and context_info.strip():
            prompt = (
                f"This is a {duration:.2f} seconds audio, with extra info: "
                f"{context_info.strip()}\n\nPlease transcribe it with these keys: "
                + ", ".join(_KEYS)
            )
        else:
            prompt = (
                f"This is a {duration:.2f} seconds audio, please transcribe it with "
                f"these keys: " + ", ".join(_KEYS)
            )
        return {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "audio_url",
                            "audio_url": {"url": f"data:audio/wav;base64,{b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
            "top_p": 1.0,
            "stream": stream,
        }

    def _post(
        self, audio_path: str, duration: float, context_info: Optional[str]
    ) -> str:
        """Blocking single-pass request; returns the raw content string."""
        payload = self._build_payload(audio_path, duration, context_info, stream=False)
        resp = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            timeout=self.request_timeout,
        )
        if resp.status_code != 200:
            logger.error(
                f"VibeVoice vLLM transcription failed: HTTP {resp.status_code} from "
                f"{self.base_url} — {resp.text[:300]}"
            )
            raise RuntimeError(
                f"vLLM returned HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()["choices"][0]["message"]["content"]

    def _parse_and_build(self, content: str, duration: float) -> TranscriptionResult:
        """Parse the vLLM JSON transcript into a TranscriptionResult, applying the
        repetition-loop hardening and routing any degeneration to System Errors."""
        if any(m in content for m in _TEMPLATE_MARKERS):
            logger.error(
                "VibeVoice vLLM decoder degeneration: chat-template tokens leaked into "
                "single-pass output (catastrophic generation failure). Transcription unusable."
            )
            raise RuntimeError(
                "VibeVoice vLLM output contained raw template tokens (degeneration)"
            )

        cleaned = re.sub(
            r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE
        ).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(
                f"VibeVoice vLLM structured decode failed ({e}) on single-pass output "
                f"(JSON overflow / unterminated). Transcription unusable."
            )
            raise RuntimeError(f"VibeVoice vLLM JSON parse failed: {e}")
        if isinstance(data, dict):
            data = data.get("segments") or data.get("result") or [data]

        segments: list[Segment] = []
        speakers_map: dict[str, tuple[float, float]] = {}
        text_parts: list[str] = []
        loops_collapsed = 0
        worst_run = 0
        nondict_skipped = 0

        for seg in data:
            if not isinstance(seg, dict):
                nondict_skipped += 1
                continue
            raw_text = str(seg.get("Content", seg.get("content", ""))).strip()
            text, longest_run = self._collapse_loops(raw_text)
            if longest_run >= self._LOOP_REPORT_RUN:
                loops_collapsed += 1
                worst_run = max(worst_run, longest_run)
            start = float(
                seg.get("Start time", seg.get("Start", seg.get("start", 0.0)))
            )
            end = float(seg.get("End time", seg.get("End", seg.get("end", 0.0))))
            speaker_raw = seg.get("Speaker ID", seg.get("Speaker", seg.get("speaker")))
            speaker_id = f"Speaker {speaker_raw}" if speaker_raw is not None else None

            if text:
                text_parts.append(text)
            segments.append(
                Segment(text=text, start=start, end=end, speaker=speaker_id)
            )
            if speaker_id:
                if speaker_id not in speakers_map:
                    speakers_map[speaker_id] = (start, end)
                else:
                    ps, pe = speakers_map[speaker_id]
                    speakers_map[speaker_id] = (min(ps, start), max(pe, end))

        if loops_collapsed:
            logger.error(
                f"VibeVoice vLLM decoder degeneration: collapsed {loops_collapsed} "
                f"repetition loop(s) (longest run {worst_run} tokens) in single-pass "
                f"output. Recovered via post-decode loop collapse."
            )
        if nondict_skipped:
            logger.error(
                f"VibeVoice vLLM malformed output: skipped {nondict_skipped} non-dict "
                f"segment element(s) from a partial JSON parse."
            )

        speakers = [
            Speaker(id=sid, start=t[0], end=t[1]) for sid, t in speakers_map.items()
        ]
        full_text = " ".join(text_parts) if text_parts else ""
        out_duration = max((s.end for s in segments), default=duration)
        logger.info(
            f"VibeVoice vLLM single-pass: {len(full_text)} chars, {len(segments)} "
            f"segments, {len(speakers)} speakers"
        )
        return TranscriptionResult(
            text=full_text,
            words=[],
            segments=segments,
            speakers=speakers if speakers else None,
            language=None,
            duration=out_duration,
        )

    # ------------------------------------------------------------------ public API
    def transcribe(
        self, audio_file_path: str, context_info: Optional[str] = None
    ) -> TranscriptionResult:
        """Single-pass transcription (blocking)."""
        duration = _audio_duration(audio_file_path)
        logger.info(f"VibeVoice vLLM transcribing {duration:.1f}s single-pass")
        content = self._post(audio_file_path, duration, context_info)
        return self._parse_and_build(content, duration)

    def transcribe_with_progress(
        self, audio_file_path: str, hotwords: Optional[str] = None
    ):
        """Single-pass with NDJSON heartbeats so the backend read stays alive.

        Runs the blocking request in a background thread and emits a progress event
        every ``heartbeat_secs`` until it returns, then yields the final result.

        Yields:
            {"type": "progress", "current": i, "total": 0}
            {"type": "result", "result": TranscriptionResult}
        """
        duration = _audio_duration(audio_file_path)
        logger.info(
            f"VibeVoice vLLM transcribing {duration:.1f}s single-pass (progress)"
        )
        holder: dict = {}

        def _work():
            try:
                holder["content"] = self._post(audio_file_path, duration, hotwords)
            except Exception as e:  # noqa: BLE001 — surfaced after join
                holder["error"] = e

        thread = threading.Thread(target=_work, daemon=True)
        thread.start()
        beat = 0
        while thread.is_alive():
            thread.join(timeout=self.heartbeat_secs)
            if thread.is_alive():
                beat += 1
                yield {"type": "progress", "current": beat, "total": 0}

        if "error" in holder:
            raise holder["error"]
        result = self._parse_and_build(holder["content"], duration)
        yield {"type": "result", "result": result}
