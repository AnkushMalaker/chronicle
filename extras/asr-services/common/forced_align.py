"""
Word-level forced alignment via torchaudio's MMS_FA bundle.

Provider-agnostic helper that takes a known transcript + the audio and returns
real per-word start/end times (the WhisperX pattern: ASR gives the text, a CTC
acoustic model gives the timing). Used by ASR providers whose own timestamps are
unreliable (e.g. Granite's mod-1000 ``[T:N]`` tags saturate on long windows).

The acoustic model is MMS_FA ("Scaling Speech Technology to 1,000+ Languages",
1100+ languages, CTC) — already available through torchaudio, so no extra pip
dependency. It works on Latin-script languages out of the box after a simple
normalization (lowercase, strip accents/punctuation), which covers Granite's
en/fr/de/es/pt.
"""

import logging
import re
import unicodedata

import torch

logger = logging.getLogger(__name__)

# MMS_FA's character token set is [a-z'] (+ a `<star>` token for unknowns). We
# normalize each word to that set before alignment; words that reduce to empty
# (digits, pure punctuation) are skipped and inherit a neighbour's timing.
_KEEP_RE = re.compile(r"[^a-z']")


def normalize_for_alignment(word: str) -> str:
    """Lowercase, strip accents to ASCII (é→e, ñ→n …), keep only [a-z']."""
    decomposed = (
        unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode("ascii")
    )
    return _KEEP_RE.sub("", decomposed.lower())


class ForcedAligner:
    """MMS_FA word-level forced aligner. Load once, reuse across requests."""

    def __init__(self, device: str | None = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = None
        self.tokenizer = None
        self.aligner = None
        self.sample_rate = 16000

    def load(self) -> None:
        from torchaudio.pipelines import MMS_FA as bundle

        logger.info("Loading MMS_FA forced-alignment model...")
        self.model = bundle.get_model().to(self.device)
        self.model.eval()
        self.tokenizer = bundle.get_tokenizer()
        self.aligner = bundle.get_aligner()
        self.sample_rate = bundle.sample_rate
        logger.info(f"MMS_FA aligner loaded on {self.device} (sr={self.sample_rate})")

    def align(
        self, waveform: torch.Tensor, words: list[str]
    ) -> list[tuple[float, float] | None]:
        """Align ``words`` to ``waveform`` (1×N, 16 kHz mono float).

        Returns a list parallel to ``words`` of (start_s, end_s) per word, or
        ``None`` for words that couldn't be tokenized (e.g. digits/punctuation).
        """
        if self.model is None:
            raise RuntimeError("Aligner not loaded. Call load() first.")
        if waveform.dim() == 1:
            waveform = waveform.unsqueeze(0)

        normalized = [normalize_for_alignment(w) for w in words]
        keep_idx = [i for i, n in enumerate(normalized) if n]
        times: list[tuple[float, float] | None] = [None] * len(words)
        if not keep_idx:
            return times

        tokens = [normalized[i] for i in keep_idx]
        try:
            with torch.inference_mode():
                emission, _ = self.model(waveform.to(self.device))
                token_spans = self.aligner(emission[0], self.tokenizer(tokens))

            # Seconds per emission frame: total samples / num frames / sample rate.
            sec_per_frame = waveform.size(1) / emission.size(1) / self.sample_rate
            for k, spans in enumerate(token_spans):
                orig_i = keep_idx[k]
                start = spans[0].start * sec_per_frame
                end = spans[-1].end * sec_per_frame
                times[orig_i] = (float(start), float(end))
        except RuntimeError as e:
            # CTC forced alignment requires emission length >= total tokens +
            # repeats. A very dense window (many words for its duration, e.g. fast
            # code-mixed speech, or a hallucinated/repetitive run) violates this and
            # torchaudio raises "targets length is too long for CTC". Aborting here
            # would propagate up and kill the entire (possibly hour-long) batch
            # transcription mid-stream — the connection-drop / "transcription
            # failed" symptom. Instead, degrade gracefully: keep the text and give
            # the words evenly-distributed timings across the window so ordering and
            # approximate timing survive.
            logger.warning(
                f"Forced alignment failed ({e}); falling back to proportional "
                f"timings for {len(keep_idx)} word(s) over "
                f"{waveform.size(1) / self.sample_rate:.1f}s window"
            )
            window_dur = waveform.size(1) / self.sample_rate
            n = len(keep_idx)
            step = window_dur / n if n else 0.0
            for k, orig_i in enumerate(keep_idx):
                times[orig_i] = (float(k * step), float((k + 1) * step))
        return times
