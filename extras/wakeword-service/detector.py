"""Acoustic wake-word detector + end-of-turn capture (multi-wake-word).

Wraps, per client, N independent wake models plus a shared end-of-turn stack:

- one ``NanoInterpreter`` per wake word (e.g. ``hey_hermes`` + ``hermes``),
  scored in parallel on every frame — acoustic wake words.
- Silero VAD (``SileroOnnxModel``) — gates speech for end-of-turn.
- Smart Turn v3 (``LocalSmartTurnAnalyzerV3``) — semantic end-of-turn decision.

Per-client arming state lives in :class:`ClientWakeState`, keyed by client_id
in the consumer. Audio frames arrive as int16 PCM at 16 kHz.

Flow per client:
  1. Feed every frame to EACH wake interpreter. The first word to satisfy its
     patience/threshold (in config-priority order) ARMS — a single arm per
     debounce window across all words (a phrase like "hey hermes" that trips
     several models dispatches once). Co-firing words are recorded as
     ``also_fired`` metadata, never a second capture.
  2. While armed, run Silero VAD per 512-sample sub-frame and buffer it into the
     Smart Turn analyzer. At each speech->silence pause, query the Smart Turn
     MODEL (analyze_end_of_turn) for the semantic end-of-turn decision; on
     COMPLETE the turn is captured -> emit. The analyzer's own stop_secs silence
     timer is kept only as a LONG backstop if the model never fires.
  3. A max-arm-duration guard ends capture even if EOT never fires.

Because the wake words can overlap acoustically (``hermes`` is a substring of
``hey hermes``), clean per-word POSITIVE data comes from the "prime + say it"
enrollment flow, where the user declares which word they are recording. Live
arms are attributed to the single arming word (the shorter word may occasionally
win a frame early for an overlapping phrase — that only affects which review
queue the trigger clip lands in, which a human reviews, not dispatch).
"""

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from nanowakeword import NanoInterpreter


def _load_interp(path: str):
    """Pick the wake backend by model file: ``.pt`` -> HuBERT+conv-attn (GPU),
    else the stock nanowakeword ONNX interpreter. Both expose the same
    ``load_model``/``predict``/``reset``/``models`` surface. The HuBERT backend
    (and its heavy torch import) is loaded LAZILY — only when a ``.pt`` model is
    configured — so the stock CPU image never needs torch."""
    if path.endswith(".pt"):
        from hubert_detector import HubertInterpreter

        return HubertInterpreter.load_model(path)
    return NanoInterpreter.load_model(path)


from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroOnnxModel
from verifier import HubertVerifier, WakeVerifier

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
VAD_FRAME_SAMPLES = 512  # Silero requires exactly 512 samples @ 16 kHz
# nanowakeword's streaming feature pipeline only scores correctly on exactly
# 1280-sample (80 ms) frames. Feeding the raw 0.25 s / 4000-sample Redis chunks
# directly yields a flat 0.0 score — the live frames MUST be reframed to 1280.
WAKE_FRAME_SAMPLES = 1280

# The wake model scores a 16-embedding-frame window = 80 ms stride × 15 + 760 ms
# embedding window = 1.96 s of audio (its receptive field). A captured clip must
# cover at least this to reproduce a detection; cold-streaming reproduction is
# also alignment-sensitive, so we keep a margin.
RECEPTIVE_FIELD_SECONDS = 1.96
# Rolling pre-roll kept per client so that on an arm we can snapshot the audio
# that *caused* it (the wake-word window) for false-positive review — separate
# from the command turn that follows. Sized to 3 s (> the 1.96 s receptive field
# + lead-in) so the snapshot reproduces the detection standalone; the model
# activation itself is the LAST ~1.96 s of the clip (the arm fires at its end).
PREROLL_SECONDS = 3.0
PREROLL_SAMPLES = int(PREROLL_SECONDS * SAMPLE_RATE)
# Lead-in pulled from the pre-roll when a primed positive capture starts, so the
# very start of the utterance isn't clipped at speech onset.
PRIME_LEADIN_SAMPLES = int(0.3 * SAMPLE_RATE)


@dataclass
class ClientWakeState:
    """Per-client wake-word + capture state (in-memory, v1)."""

    armed: bool = False
    # Which wake word armed the current capture (None when not armed).
    armed_wakeword: Optional[str] = None
    # Other wake words that also scored over threshold at the arm instant.
    also_fired: list = field(default_factory=list)
    arm_time: float = 0.0
    arm_score: float = 0.0
    # Single shared arm gate: one arm per debounce window across all DISPATCH words.
    last_detection_time: float = 0.0
    # Separate debounce for collect-only (shadow) firings, so they neither spam
    # the review queue nor touch the real-arm debounce of dispatch words.
    last_collect_time: float = 0.0
    # Per-wake-word consecutive-frame-over-threshold counters (patience).
    consec: dict = field(default_factory=dict)
    # Smart Turn analyzer is per-client (it holds an audio buffer + thread).
    turn_analyzer: Optional[LocalSmartTurnAnalyzerV3] = None
    vad_model: Optional[SileroOnnxModel] = None
    # One wake interpreter PER wake word. Each NanoInterpreter holds a STREAMING
    # feature buffer (raw audio -> mel -> 96-d embeddings) spanning ~2 s of
    # context, so they are per-client and never shared across streams.
    interpreters: dict = field(default_factory=dict)
    # Leftover PCM samples not yet aligned to a 512-sample VAD frame.
    vad_remainder: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.int16)
    )
    # Leftover PCM not yet aligned to a 1280-sample wake-interpreter frame
    # (shared across wake words — the reframing is identical for all).
    wake_remainder: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.int16)
    )
    # Raw int16 PCM of the armed command turn (arm -> EOT), for batch ASR.
    capture_chunks: list = field(default_factory=list)
    # Count of VAD speech frames seen during this armed capture. Used to gate the
    # backend's batch ASR: near-silent captures (a false arm with nothing spoken)
    # make self-diarizing ASR hallucinate, so we flag them and skip transcription.
    capture_speech_frames: int = 0
    # Semantic end-of-turn (Smart Turn) tracking within an armed capture: have we
    # heard speech yet, how many consecutive silent VAD frames since, and the
    # silence-frame count at which to next query the Smart Turn model.
    eot_speech_seen: bool = False
    eot_silence_frames: int = 0
    eot_next_check: int = 0
    # Rolling recent audio (samples) for snapshotting the wake-trigger window.
    preroll: deque = field(default_factory=deque)
    preroll_len: int = 0
    # Snapshot of the trigger window taken at arm time (for false-positive review).
    trigger_audio: bytes = b""
    # Dev capture: interpreter buffer state snapshotted at arm (of the arming
    # word's interpreter). The model's ~2 s receptive field exceeds the 3 s
    # trigger_audio window's usable part, so these make an arm exactly
    # reproducible offline: trigger_features = the (N, 96) embedding buffer the
    # wake model scored on; trigger_context = the full ~10 s raw-audio buffer.
    trigger_features: Optional[np.ndarray] = None
    trigger_context: bytes = b""
    # --- "prime + say it" positive-capture mode (data collection) ---
    priming: bool = False
    # Which wake word the user declared they are enrolling for this prime.
    prime_wakeword: Optional[str] = None
    prime_start: float = 0.0  # monotonic; whole-session hard-cap reference
    prime_stop_requested: bool = False  # manual "end now" from the UI
    prime_speech_started: bool = False
    prime_chunks: list = field(default_factory=list)
    prime_silence_run: int = 0  # consecutive silent VAD frames after speech began


@dataclass
class WakeEvent:
    """Emitted when a turn is captured.

    Two kinds:
      - ``command``: a real acoustic arm + captured command turn (the live wake
        path). ``audio`` is the command; ``trigger_audio`` is the wake-word window
        snapshotted at arm, saved for false-positive review.
      - ``primed_positive``: a "prime + say it" data-collection capture. ``audio``
        is the spoken wake-word utterance; ``score`` is the model's max score over
        it and ``is_false_negative`` is True when that fell below threshold.

    ``wakeword`` is the word this event belongs to (the arming word for commands,
    the declared word for primes). ``also_fired`` lists other words over threshold
    at arm (command kind only) — recorded for visibility, never cross-written.
    """

    client_id: str
    session_id: str
    wakeword: str
    # Raw int16 PCM @16k of the captured turn (command, or primed wake utterance).
    audio: bytes
    arm_time: float
    eot_time: float
    score: float
    reason: str  # "smart_turn" | "max_duration" | "stream_end" | "primed" | ...
    kind: str = "command"  # "command" | "primed_positive"
    also_fired: list = field(default_factory=list)
    # Collect-only (shadow) firing: the model fired but the word is in collect-only
    # mode — snapshot the trigger window for FP review, but do NOT dispatch to the
    # plugin, play a tone, or capture a command turn. (command kind only.)
    collect_only: bool = False
    # Whether VAD heard enough speech in the captured turn to be worth batch-ASR.
    # False for near-silent false arms — the backend skips transcription so the
    # ASR can't hallucinate a phantom command. (command kind only.)
    has_speech: bool = True
    # Wake-trigger window captured at arm (command kind only), for FP review.
    trigger_audio: bytes = b""
    # primed_positive only: did the live model under-score this true positive?
    is_false_negative: bool = False
    # Dev capture (command kind): interpreter buffer state at arm, so the arm is
    # reproducible offline. buffer_features = (N, 96) embeddings the wake model
    # scored on; buffer_context = full ~10 s raw-audio buffer (int16 PCM bytes).
    buffer_features: Optional[np.ndarray] = None
    buffer_context: bytes = b""


class HermesDetector:
    """Loads N wake models + builds per-client capture state."""

    def __init__(
        self,
        models: dict[str, str],
        threshold: float = 0.9,
        patience: int = 5,
        debounce_secs: float = 3.0,
        vad_threshold: float = 0.5,
        stop_secs: float = 2.0,
        max_arm_secs: float = 15.0,
        smart_turn_model_path: Optional[str] = None,
        silero_vad_model_path: Optional[str] = None,
        eot_min_silence_secs: float = 0.2,
        eot_recheck_secs: float = 0.3,
        prime_timeout_secs: float = 10.0,
        prime_trail_silence_secs: float = 0.6,
        prime_max_secs: float = 4.0,
        prime_vad_threshold: float = 0.3,
        min_command_speech_secs: float = 0.3,
        verifiers: Optional[dict[str, str]] = None,
        verifier_threshold: Optional[float] = None,
        thresholds: Optional[dict[str, float]] = None,
        patiences: Optional[dict[str, int]] = None,
        collect_only: Optional[list[str]] = None,
        verifiers_disabled: Optional[list[str]] = None,
    ):
        """Initialize the detector.

        Args:
            models: Ordered ``{wakeword: onnx_path}``. Insertion order is the
                arming PRIORITY when several words fire on the same frame (put the
                more specific / lower-FP word first).
            threshold: Default acoustic detection threshold (per-word override via
                ``thresholds``). Favor precision.
            verifiers: Optional ``{wakeword: verifier_npz_path}`` second-stage
                verifiers (``.npz`` from ``training/train_verifier.py``). When set
                for a word, each of its arms is confirmed by the verifier before it
                dispatches; arms it judges false are dropped. Missing word -> no
                verifier (stage-1 only) for that word.
            verifier_threshold: Optional override of every verifier's trained
                operating-point threshold.
            patience: Default consecutive-frames-over-threshold required before
                firing — the main false-positive suppressor (per-word override via
                ``patiences``).
            thresholds: Optional per-word threshold overrides.
            patiences: Optional per-word patience overrides.
            debounce_secs: Suppress repeat arming within this window (shared
                across all wake words — one arm per window).
            vad_threshold: Silero speech-probability threshold.
            stop_secs: LONG backstop — silence (s) that forces end-of-turn if the
                Smart Turn model never fires. End-of-turn is normally decided by
                the model at each pause, not by this timer.
            max_arm_secs: Hard cap on capture duration after arming.
            eot_min_silence_secs: Silence after speech before the first Smart Turn
                model query (the endpoint gap).
            eot_recheck_secs: How often to re-query the model while silence
                continues after an INCOMPLETE verdict.
            smart_turn_model_path: Optional override for the Smart Turn ONNX. If
                None, the analyzer loads its own pipecat-bundled copy.
            silero_vad_model_path: Optional override for the Silero VAD ONNX. If
                None, the pipecat-bundled copy is resolved automatically.
        """
        if not models:
            raise ValueError("HermesDetector needs at least one wake model")
        self.models = dict(models)
        self.wakewords = list(self.models.keys())  # config order = priority
        # Words in "collect-only" (shadow) mode fire to gather false-positive
        # review data but never dispatch a command / play a tone / block a real
        # wake word — used to farm FPs live for a not-yet-trusted word.
        self.collect_only = set(collect_only or [])
        # Words whose second-stage verifier is toggled OFF at runtime (from the
        # Wake-Word Lab). The verifier stays LOADED — this only skips its check so
        # arms dispatch on the stage-1 model alone. Mutable; flipped via the UI.
        self.verifiers_disabled = set(verifiers_disabled or [])
        self.threshold = threshold
        self.patience = patience
        self.thresholds = {
            w: (thresholds or {}).get(w, threshold) for w in self.wakewords
        }
        self.patiences = {w: (patiences or {}).get(w, patience) for w in self.wakewords}
        self.debounce_secs = debounce_secs
        # Diagnostic: log any frame scoring above this floor (0 = off).
        self.score_log_floor = float(os.getenv("WAKEWORD_SCORE_LOG", "0") or 0)
        self.vad_threshold = vad_threshold
        self.stop_secs = stop_secs
        self.max_arm_secs = max_arm_secs
        # Endpoint gap (in VAD frames) of silence after speech before the first
        # Smart Turn query, then how often to re-query while silence continues.
        self.eot_min_silence_frames = max(
            1, int(eot_min_silence_secs * SAMPLE_RATE / VAD_FRAME_SAMPLES)
        )
        self.eot_recheck_frames = max(
            1, int(eot_recheck_secs * SAMPLE_RATE / VAD_FRAME_SAMPLES)
        )
        self.prime_timeout_secs = prime_timeout_secs
        self.prime_trail_silence_frames = max(
            1, int(prime_trail_silence_secs * SAMPLE_RATE / VAD_FRAME_SAMPLES)
        )
        self.prime_max_samples = int(prime_max_secs * SAMPLE_RATE)
        # While priming we drop the VAD gate well below the live threshold so the
        # primed utterance is auto-caught even if spoken softly — the whole point
        # of "I'll say it now" is that we already know speech is coming.
        self.prime_vad_threshold = prime_vad_threshold
        # Minimum cumulative VAD speech (in frames) a captured command must contain
        # to be sent for batch ASR. Below this the turn is treated as a near-silent
        # false arm and the backend skips transcription (avoids ASR hallucination).
        self.min_command_speech_frames = max(
            1, int(min_command_speech_secs * SAMPLE_RATE / VAD_FRAME_SAMPLES)
        )
        self.smart_turn_model_path = smart_turn_model_path

        # Interpreters are per-client (built in new_client_state) — the streaming
        # feature buffer is stateful and must not be shared across streams. Load a
        # throwaway probe per word here only to validate it and read its key.
        self._wake_keys: dict[str, str] = {}
        self._wake_in_names: dict[str, str] = {}
        for wakeword, path in self.models.items():
            logger.info(
                f"Loading wake model '{wakeword}': {path} "
                f"(threshold={self.thresholds[wakeword]}, "
                f"patience={self.patiences[wakeword]})"
            )
            probe = _load_interp(path)
            key = next(iter(probe.models.keys()))
            self._wake_keys[wakeword] = key
            # Cache the wake ONNX input name so the verifier can score raw feature
            # windows directly (it picks the peak window the model armed on).
            self._wake_in_names[wakeword] = probe.models[key].get_inputs()[0].name
            del probe
        logger.info(
            f"Loaded {len(self.wakewords)} wake model(s): {', '.join(self.wakewords)} "
            f"(per-client interpreters)"
        )

        # Optional second-stage verifiers (FP suppression), one per word. Loaded
        # once, shared read-only across clients (pure-numpy scoring, no state).
        self.verifiers: dict[str, WakeVerifier | HubertVerifier] = {}
        for wakeword, vpath in (verifiers or {}).items():
            if wakeword not in self.models:
                logger.warning(f"verifier for unknown word '{wakeword}' — ignored")
                continue
            if vpath and os.path.exists(vpath):
                # HuBERT words (.pt) score a 768-d HuBERT arm-window embedding;
                # nanowakeword words score the 96-d Google window. Pick the matching
                # verifier (both pure-numpy, share the folded-logreg .npz schema).
                if self.models[wakeword].endswith(".pt"):
                    self.verifiers[wakeword] = HubertVerifier(
                        vpath, threshold=verifier_threshold
                    )
                else:
                    self.verifiers[wakeword] = WakeVerifier(
                        vpath, threshold=verifier_threshold
                    )
                logger.info(f"verifier enabled for '{wakeword}' ({vpath})")
            elif vpath:
                logger.warning(
                    f"verifier_path '{vpath}' for '{wakeword}' not found — "
                    f"stage-1 only for that word"
                )
        if not self.verifiers:
            logger.info("No verifiers configured — running stage-1 (wake models) only")

        # Resolve the Silero VAD ONNX path once (explicit override or bundled).
        if silero_vad_model_path:
            self._vad_model_path = silero_vad_model_path
        else:
            import importlib.resources as ir

            self._vad_model_path = str(
                ir.files("pipecat.audio.vad.data").joinpath("silero_vad.onnx")
            )

    def new_client_state(self) -> ClientWakeState:
        """Build fresh per-client state: one interpreter per wake word + VAD + ST."""
        analyzer = LocalSmartTurnAnalyzerV3(
            smart_turn_model_path=self.smart_turn_model_path,
            params=SmartTurnParams(stop_secs=self.stop_secs),
        )
        analyzer.set_sample_rate(SAMPLE_RATE)
        return ClientWakeState(
            interpreters={w: _load_interp(p) for w, p in self.models.items()},
            consec={w: 0 for w in self.wakewords},
            turn_analyzer=analyzer,
            vad_model=SileroOnnxModel(self._vad_model_path, force_onnx_cpu=True),
        )

    async def process_frame(
        self, state: ClientWakeState, client_id: str, session_id: str, pcm: bytes
    ) -> Optional[WakeEvent]:
        """Process one PCM frame for a client; returns a WakeEvent on capture.

        Args:
            state: This client's wake state.
            client_id: Client identifier.
            session_id: Audio session id (== client_id in this pipeline).
            pcm: Raw int16 PCM bytes (any length; 0.25 s = 8000 bytes typical).

        Returns:
            A WakeEvent when a turn is captured, else None.
        """
        audio = np.frombuffer(pcm, dtype=np.int16)
        if audio.size == 0:
            return None

        # Keep recent audio so an arm can snapshot the window that triggered it
        # (and so a primed capture has a short lead-in before speech onset).
        self._push_preroll(state, audio)

        # "Prime + say it" data-collection mode takes precedence over arming.
        if state.priming:
            return self._run_prime(state, client_id, session_id, audio)

        if not state.armed:
            # _run_wake arms dispatch words (event comes later from capture) and
            # returns a shadow event immediately for collect-only words.
            return self._run_wake(state, client_id, session_id, audio)

        # Armed: drive VAD + Smart Turn to capture the command turn.
        return await self._run_capture(state, client_id, session_id, audio)

    def _push_preroll(self, state: ClientWakeState, audio: np.ndarray) -> None:
        """Append audio to the rolling pre-roll, trimming to PREROLL_SAMPLES."""
        state.preroll.append(audio)
        state.preroll_len += audio.size
        while state.preroll_len > PREROLL_SAMPLES and len(state.preroll) > 1:
            dropped = state.preroll.popleft()
            state.preroll_len -= dropped.size

    @staticmethod
    def _preroll_tail(state: ClientWakeState, n_samples: int) -> np.ndarray:
        """Return the last ``n_samples`` of buffered pre-roll audio."""
        if not state.preroll:
            return np.empty(0, dtype=np.int16)
        buf = np.concatenate(list(state.preroll))
        return buf[-n_samples:] if buf.size > n_samples else buf

    @staticmethod
    def _prime_leadin(state: ClientWakeState, onset_offset: int) -> np.ndarray:
        """Lead-in (PRIME_LEADIN_SAMPLES) ending exactly at the prime onset.

        The pre-roll ends at "now" (the end of the current buffer); the onset
        sits ``onset_offset`` samples before that end. We take the lead-in from
        *before* the onset rather than the most-recent 0.3 s — pulling the tail
        would duplicate the slice we immediately re-append as capture frames,
        which produced a "he-hey hermes" echo at the clip's start.
        """
        if not state.preroll:
            return np.empty(0, dtype=np.int16)
        buf = np.concatenate(list(state.preroll))
        end = buf.size - onset_offset  # index of the onset within the pre-roll
        start = max(0, end - PRIME_LEADIN_SAMPLES)
        return buf[start:end]

    def _run_wake(
        self, state: ClientWakeState, client_id: str, session_id: str, audio: np.ndarray
    ) -> Optional[WakeEvent]:
        # Reframe to exactly 1280-sample frames (carry a remainder across calls);
        # the interpreters score 0.0 on any other frame size.
        buf = (
            np.concatenate([state.wake_remainder, audio])
            if state.wake_remainder.size
            else audio
        )
        n_full = (buf.size // WAKE_FRAME_SAMPLES) * WAKE_FRAME_SAMPLES
        state.wake_remainder = buf[n_full:].copy()

        for i in range(0, n_full, WAKE_FRAME_SAMPLES):
            frame = buf[i : i + WAKE_FRAME_SAMPLES]
            now = time.monotonic()

            # Score EVERY wake word on this frame; update each patience counter.
            scores: dict[str, float] = {}
            for w in self.wakewords:
                s = state.interpreters[w].predict(frame).get(self._wake_keys[w], 0.0)
                scores[w] = s
                if self.score_log_floor and s > self.score_log_floor:
                    logger.info(f"score[{w}] {s:.3f} '{client_id}'")
                state.consec[w] = state.consec[w] + 1 if s > self.thresholds[w] else 0

            # Words ready to fire this frame, split by mode.
            ready = [w for w in self.wakewords if state.consec[w] >= self.patiences[w]]
            collect_ready = [w for w in ready if w in self.collect_only]
            dispatch_ready = [w for w in ready if w not in self.collect_only]

            # 1) Collect-only (shadow) firing: snapshot the trigger window for FP
            # review, but DON'T enter the shared armed/capture state (so it never
            # blocks a real dispatch word), don't dispatch, and don't run the
            # verifier (we want to farm what the RAW model fires on). Independent
            # debounce; only the firing word's interpreter resets (keeps dispatch
            # words warm).
            if collect_ready and (now - state.last_collect_time) > self.debounce_secs:
                cand = collect_ready[0]
                tf, tc = self._snapshot_buffers(state.interpreters[cand])
                also = [
                    w
                    for w in self.wakewords
                    if w != cand and scores[w] > self.thresholds[w]
                ]
                state.last_collect_time = now
                state.consec[cand] = 0
                state.interpreters[cand].reset()
                trig = self._preroll_tail(state, PREROLL_SAMPLES).tobytes()
                logger.info(
                    f"👁 SHADOW '{client_id}' word={cand} "
                    f"score={scores[cand]:.4f} (collect-only)"
                )
                return WakeEvent(
                    client_id=client_id,
                    session_id=session_id,
                    wakeword=cand,
                    audio=b"",
                    arm_time=now,
                    eot_time=now,
                    score=scores[cand],
                    reason="shadow_arm",
                    kind="command",
                    collect_only=True,
                    also_fired=also,
                    has_speech=False,
                    trigger_audio=trig,
                    buffer_features=tf,
                    buffer_context=tc,
                )

            # 2) Real dispatch arming: one arm per debounce window across all
            # dispatch words, highest-priority ready word that passes its verifier.
            if (
                not dispatch_ready
                or (now - state.last_detection_time) <= self.debounce_secs
            ):
                continue

            arm_word = None
            trigger_features, trigger_context = None, b""
            for cand in dispatch_ready:
                # Snapshot the interpreter's streaming buffer that produced this
                # candidate arm BEFORE any reset clears it — needed both by the
                # verifier and for false-positive review.
                tf, tc = self._snapshot_buffers(state.interpreters[cand])
                verifier = self.verifiers.get(cand)
                if verifier is not None and cand not in self.verifiers_disabled:
                    if isinstance(verifier, HubertVerifier):
                        # HuBERT words expose no Google feature buffer; the verifier
                        # scores the arm-window HuBERT embedding the interpreter just
                        # computed (the frame that tripped patience).
                        passed, vprob = verifier.verify_embedding(
                            state.interpreters[cand].arm_window_embedding()
                        )
                    elif tf is not None:
                        passed, vprob = verifier.verify(
                            tf,
                            state.interpreters[cand].models[self._wake_keys[cand]],
                            self._wake_in_names[cand],
                        )
                    else:  # no buffer to judge — fail open (never block a wake)
                        passed, vprob = True, 1.0
                    if not passed:
                        # Reject: clear THIS word's patience but keep streaming (no
                        # interpreter reset, no debounce) so a real wake right after
                        # is not delayed; let a lower-priority ready word still arm.
                        state.consec[cand] = 0
                        logger.info(
                            f"🚫 verifier REJECTED '{cand}' arm '{client_id}' "
                            f"(wake={scores[cand]:.4f} verify={vprob:.3f} "
                            f"< {verifier.threshold:.2f})"
                        )
                        continue
                    logger.info(
                        f"✅ verifier confirmed '{cand}' '{client_id}' "
                        f"(verify={vprob:.3f})"
                    )
                arm_word = cand
                trigger_features, trigger_context = tf, tc
                break

            if arm_word is None:
                continue

            # Other words also over threshold at this instant (visibility only).
            also_fired = [
                w
                for w in self.wakewords
                if w != arm_word and scores[w] > self.thresholds[w]
            ]
            state.armed = True
            state.armed_wakeword = arm_word
            state.also_fired = also_fired
            state.arm_time = now
            state.arm_score = scores[arm_word]
            state.last_detection_time = now
            for w in self.wakewords:
                state.consec[w] = 0
            # Snapshot the wake-trigger window for false-positive review (the
            # audio that *caused* this arm, distinct from the command turn).
            state.trigger_audio = self._preroll_tail(state, PREROLL_SAMPLES).tobytes()
            state.trigger_features = trigger_features
            state.trigger_context = trigger_context
            # Reset all interpreters + wake remainder so capture/next wake start clean.
            for w in self.wakewords:
                state.interpreters[w].reset()
            state.wake_remainder = np.empty(0, dtype=np.int16)
            logger.info(
                f"🔔 ARMED '{client_id}' word={arm_word} score={scores[arm_word]:.4f}"
                f"{f' also_fired={also_fired}' if also_fired else ''}"
            )
            return

    def flush(
        self, state: ClientWakeState, client_id: str, session_id: str
    ) -> Optional[WakeEvent]:
        """Finalize an in-progress capture when the stream ends while armed.

        Bounded recordings (e.g. browser push-to-record) end before Smart Turn
        can detect end-of-turn, so stream-end is itself the end of the turn.
        """
        if state.priming:
            if state.prime_speech_started:
                return self._finish_prime(
                    state, client_id, session_id, "primed_stream_end"
                )
            self._reset_prime(state)
            return None
        if state.armed:
            return self._finish_capture(state, client_id, session_id, "stream_end")
        return None

    async def _run_capture(
        self, state: ClientWakeState, client_id: str, session_id: str, audio: np.ndarray
    ) -> Optional[WakeEvent]:
        analyzer = state.turn_analyzer
        vad = state.vad_model

        # Buffer the raw command audio (arm -> EOT) for batch ASR by the backend.
        state.capture_chunks.append(audio)

        # Re-chunk into 512-sample VAD frames, carrying a remainder across calls.
        buf = (
            np.concatenate([state.vad_remainder, audio])
            if state.vad_remainder.size
            else audio
        )
        n_full = (buf.size // VAD_FRAME_SAMPLES) * VAD_FRAME_SAMPLES
        state.vad_remainder = buf[n_full:].copy()

        for i in range(0, n_full, VAD_FRAME_SAMPLES):
            frame = buf[i : i + VAD_FRAME_SAMPLES]
            conf = float(
                np.asarray(
                    vad(frame.astype(np.float32) / 32768.0, SAMPLE_RATE)
                ).flatten()[0]
            )
            is_speech = conf >= self.vad_threshold

            # Buffer the frame in the analyzer. append_audio only returns COMPLETE
            # via its own stop_secs silence counter — now a LONG backstop, not the
            # primary signal (see WAKEWORD_STOP_SECS).
            backstop = analyzer.append_audio(frame.tobytes(), is_speech)
            if backstop == EndOfTurnState.COMPLETE:
                return self._finish_capture(state, client_id, session_id, "stop_secs")

            if is_speech:
                # Speech (re)started: reset the endpoint tracking; the next query
                # happens once a fresh ``eot_min_silence_frames`` gap accrues.
                state.eot_speech_seen = True
                state.capture_speech_frames += 1
                state.eot_silence_frames = 0
                state.eot_next_check = self.eot_min_silence_frames
            elif state.eot_speech_seen:
                # Silence after speech: at the endpoint gap (then periodically while
                # silence continues) ask the Smart Turn MODEL whether the turn is
                # semantically complete. This is the real end-of-turn decision.
                state.eot_silence_frames += 1
                if state.eot_silence_frames >= state.eot_next_check:
                    model_state, _ = await analyzer.analyze_end_of_turn()
                    if model_state == EndOfTurnState.COMPLETE:
                        return self._finish_capture(
                            state, client_id, session_id, "smart_turn"
                        )
                    # INCOMPLETE: the model expects more speech — keep listening and
                    # re-query after another stretch of continued silence.
                    state.eot_next_check = (
                        state.eot_silence_frames + self.eot_recheck_frames
                    )

        # Hard cap on capture duration.
        if (time.monotonic() - state.arm_time) > self.max_arm_secs:
            return self._finish_capture(state, client_id, session_id, "max_duration")

        return None

    def _finish_capture(
        self, state: ClientWakeState, client_id: str, session_id: str, reason: str
    ) -> WakeEvent:
        eot_time = time.monotonic()
        captured = (
            np.concatenate(state.capture_chunks).tobytes()
            if state.capture_chunks
            else b""
        )
        # Silence gate: a captured turn with little/no VAD speech is a false arm
        # (nothing actually spoken after the wake word). Flag it so the backend
        # skips batch ASR — self-diarizing ASR hallucinates phantom commands on
        # near-silent audio.
        speech_secs = state.capture_speech_frames * VAD_FRAME_SAMPLES / SAMPLE_RATE
        has_speech = state.capture_speech_frames >= self.min_command_speech_frames
        event = WakeEvent(
            client_id=client_id,
            session_id=session_id,
            wakeword=state.armed_wakeword or self.wakewords[0],
            audio=captured,
            arm_time=state.arm_time,
            eot_time=eot_time,
            score=state.arm_score,
            reason=reason,
            kind="command",
            also_fired=list(state.also_fired),
            has_speech=has_speech,
            trigger_audio=state.trigger_audio,
            buffer_features=state.trigger_features,
            buffer_context=state.trigger_context,
        )
        logger.info(
            f"🛑 CAPTURED '{client_id}' word={event.wakeword} reason={reason} "
            f"dur={eot_time - state.arm_time:.2f}s audio={len(captured)}B "
            f"speech={speech_secs:.2f}s has_speech={has_speech}"
        )
        # Reset state for the next wake word.
        state.armed = False
        state.armed_wakeword = None
        state.also_fired = []
        state.arm_time = 0.0
        state.arm_score = 0.0
        state.trigger_audio = b""
        state.trigger_features = None
        state.trigger_context = b""
        state.vad_remainder = np.empty(0, dtype=np.int16)
        state.wake_remainder = np.empty(0, dtype=np.int16)
        state.capture_chunks = []
        state.capture_speech_frames = 0
        state.eot_speech_seen = False
        state.eot_silence_frames = 0
        state.eot_next_check = 0
        if state.turn_analyzer is not None:
            state.turn_analyzer.clear()
        return event

    # ------------------------------------------------------------------ #
    # "Prime + say it" positive capture (false-negative / hard-positive collection)
    # ------------------------------------------------------------------ #

    def start_priming(
        self, state: ClientWakeState, client_id: str, wakeword: str
    ) -> None:
        """Arm a one-shot positive capture: the next utterance is ``wakeword``.

        Used by the data-collection UI ("I'll say the wake word now"). The next
        VAD-detected utterance is captured as a labeled positive for ``wakeword``
        regardless of the model's score; the score (against that word's model) is
        computed afterwards to flag false negatives. Because the user declares the
        word, this enrollment path is unambiguous even when wake words overlap.
        """
        if wakeword not in self.models:
            raise ValueError(f"unknown wake word '{wakeword}' (have {self.wakewords})")
        state.priming = True
        state.prime_wakeword = wakeword
        state.prime_start = time.monotonic()
        state.prime_stop_requested = False
        state.prime_speech_started = False
        state.prime_chunks = []
        state.prime_silence_run = 0
        state.vad_remainder = np.empty(0, dtype=np.int16)
        if state.turn_analyzer is not None:
            state.turn_analyzer.clear()
        logger.info(
            f"🎯 PRIMED positive capture for '{client_id}' word={wakeword} "
            f"— awaiting speech"
        )

    def stop_priming(self, state: ClientWakeState) -> None:
        """Request a manual end of an in-progress prime (the UI 'stop' button).

        The stream task finalizes on its next frame, saving whatever was heard so
        far (or a short pre-roll fallback) so the attempt always lands in review.
        """
        if state.priming:
            state.prime_stop_requested = True

    def _run_prime(
        self, state: ClientWakeState, client_id: str, session_id: str, audio: np.ndarray
    ) -> Optional[WakeEvent]:
        # Manual "end now" from the UI: finalize immediately with whatever we have.
        if state.prime_stop_requested:
            return self._finish_prime(
                state, client_id, session_id, "primed_manual_stop"
            )

        vad = state.vad_model
        buf = (
            np.concatenate([state.vad_remainder, audio])
            if state.vad_remainder.size
            else audio
        )
        n_full = (buf.size // VAD_FRAME_SAMPLES) * VAD_FRAME_SAMPLES
        state.vad_remainder = buf[n_full:].copy()

        for i in range(0, n_full, VAD_FRAME_SAMPLES):
            frame = buf[i : i + VAD_FRAME_SAMPLES]
            conf = float(
                np.asarray(
                    vad(frame.astype(np.float32) / 32768.0, SAMPLE_RATE)
                ).flatten()[0]
            )
            # Lowered gate (prime_vad_threshold) — we know speech is coming, so
            # auto-catch it even when spoken softly.
            is_speech = conf >= self.prime_vad_threshold

            if not state.prime_speech_started:
                if is_speech:
                    # Seed with a short lead-in so the onset isn't clipped. The
                    # lead-in is taken from BEFORE the onset (onset sits buf.size-i
                    # samples before the pre-roll's end) so we don't re-include the
                    # slice we're about to append as frames (the "he-hey" echo).
                    state.prime_speech_started = True
                    state.prime_chunks = [
                        self._prime_leadin(state, buf.size - i),
                        frame,
                    ]
                    state.prime_silence_run = 0
                continue

            state.prime_chunks.append(frame)
            if is_speech:
                state.prime_silence_run = 0
            else:
                state.prime_silence_run += 1
                if state.prime_silence_run >= self.prime_trail_silence_frames:
                    return self._finish_prime(state, client_id, session_id, "primed")

        if state.prime_speech_started:
            captured = sum(c.size for c in state.prime_chunks)
            if captured >= self.prime_max_samples:
                return self._finish_prime(
                    state, client_id, session_id, "primed_max_duration"
                )

        # Whole-session hard cap: stop within prime_timeout_secs no matter what and
        # ALWAYS finalize (never a silent drop) so the attempt lands in review —
        # captured speech if we heard any, else a short pre-roll fallback.
        if time.monotonic() - state.prime_start > self.prime_timeout_secs:
            reason = (
                "primed_timeout" if state.prime_speech_started else "primed_no_speech"
            )
            return self._finish_prime(state, client_id, session_id, reason)
        return None

    def _finish_prime(
        self, state: ClientWakeState, client_id: str, session_id: str, reason: str
    ) -> WakeEvent:
        wakeword = state.prime_wakeword or self.wakewords[0]
        if state.prime_chunks:
            captured = np.concatenate(state.prime_chunks)
        else:
            # Manual stop / timeout with no VAD-gated speech: fall back to the
            # recent pre-roll so the attempt still surfaces for review instead of
            # vanishing (the user explicitly asked for it to always show up).
            captured = self._preroll_tail(state, PREROLL_SAMPLES)
        interp = state.interpreters[wakeword]
        score = self._score_buffer(interp, wakeword, captured) if captured.size else 0.0
        is_fn = score < self.thresholds[wakeword]
        event = WakeEvent(
            client_id=client_id,
            session_id=session_id,
            wakeword=wakeword,
            audio=captured.tobytes(),
            arm_time=0.0,
            eot_time=time.monotonic(),
            score=score,
            reason=reason,
            kind="primed_positive",
            is_false_negative=is_fn,
        )
        logger.info(
            f"🎯 PRIMED POSITIVE '{client_id}' word={wakeword} score={score:.4f} "
            f"{'(FALSE NEGATIVE)' if is_fn else '(would-have-fired)'} "
            f"dur={captured.size / SAMPLE_RATE:.2f}s reason={reason}"
        )
        self._reset_prime(state)
        return event

    def _score_buffer(self, interp, wakeword: str, audio: np.ndarray) -> float:
        """Max wake score over a buffer using this word's interpreter (reset
        before/after so the live streaming state isn't polluted)."""
        key = self._wake_keys[wakeword]
        interp.reset()
        best = 0.0
        n_full = (audio.size // WAKE_FRAME_SAMPLES) * WAKE_FRAME_SAMPLES
        for i in range(0, n_full, WAKE_FRAME_SAMPLES):
            s = interp.predict(audio[i : i + WAKE_FRAME_SAMPLES]).get(key, 0.0)
            best = max(best, s)
        interp.reset()
        return float(best)

    @staticmethod
    def _snapshot_buffers(interp) -> tuple:
        """Copy the interpreter's streaming buffers at arm time so the arm is
        reproducible offline. Returns (feature_buffer copy as (N, 96) float32,
        raw-audio buffer as int16 PCM bytes). Never raises — capture must not
        break detection."""
        try:
            pre = interp.preprocessor
            feats = np.asarray(pre.feature_buffer, dtype=np.float32).copy()
            raw = np.asarray(pre.raw_data_buffer, dtype=np.int16).tobytes()
            return feats, raw
        except Exception as e:  # noqa: BLE001
            logger.warning(f"buffer-state snapshot failed: {e}")
            return None, b""

    @staticmethod
    def _reset_prime(state: ClientWakeState) -> None:
        state.priming = False
        state.prime_wakeword = None
        state.prime_start = 0.0
        state.prime_stop_requested = False
        state.prime_speech_started = False
        state.prime_chunks = []
        state.prime_silence_run = 0
        state.vad_remainder = np.empty(0, dtype=np.int16)
        if state.turn_analyzer is not None:
            state.turn_analyzer.clear()
