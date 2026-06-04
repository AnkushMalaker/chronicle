"""Acoustic Hermes wake-word detector + end-of-turn capture.

Wraps three ONNX models, all run standalone (no pipecat pipeline runtime):

- ``hermes.onnx`` via nanowakeword ``NanoInterpreter`` — acoustic wake word.
- Silero VAD (``SileroOnnxModel``) — gates speech for end-of-turn.
- Smart Turn v3 (``LocalSmartTurnAnalyzerV3``) — semantic end-of-turn decision.

Per-client arming state lives in :class:`ClientWakeState`, keyed by client_id
in the consumer. Audio frames arrive as int16 PCM at 16 kHz.

Flow per client:
  1. Feed every frame to the wake interpreter. score > threshold -> ARM.
  2. While armed, run Silero VAD per 512-sample sub-frame and buffer it into the
     Smart Turn analyzer. At each speech->silence pause, query the Smart Turn
     MODEL (analyze_end_of_turn) for the semantic end-of-turn decision; on
     COMPLETE the turn is captured -> emit. The analyzer's own stop_secs silence
     timer is kept only as a LONG backstop if the model never fires.
  3. A max-arm-duration guard ends capture even if EOT never fires.
"""

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from nanowakeword import NanoInterpreter
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroOnnxModel

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
VAD_FRAME_SAMPLES = 512  # Silero requires exactly 512 samples @ 16 kHz
# nanowakeword's streaming feature pipeline only scores correctly on exactly
# 1280-sample (80 ms) frames. Feeding the raw 0.25 s / 4000-sample Redis chunks
# directly yields a flat 0.0 score — the live frames MUST be reframed to 1280.
WAKE_FRAME_SAMPLES = 1280

# Rolling pre-roll kept per client so that on an arm we can snapshot the audio
# that *caused* it (the wake-word window) for false-positive review — separate
# from the command turn that follows.
PREROLL_SECONDS = 1.5
PREROLL_SAMPLES = int(PREROLL_SECONDS * SAMPLE_RATE)
# Lead-in pulled from the pre-roll when a primed positive capture starts, so the
# very start of the utterance isn't clipped at speech onset.
PRIME_LEADIN_SAMPLES = int(0.3 * SAMPLE_RATE)


@dataclass
class ClientWakeState:
    """Per-client wake-word + capture state (in-memory, v1)."""

    armed: bool = False
    arm_time: float = 0.0
    arm_score: float = 0.0
    last_detection_time: float = 0.0
    # Consecutive 1280-frames whose raw wake score exceeds threshold (patience).
    consec: int = 0
    # Smart Turn analyzer is per-client (it holds an audio buffer + thread).
    turn_analyzer: Optional[LocalSmartTurnAnalyzerV3] = None
    vad_model: Optional[SileroOnnxModel] = None
    # Leftover PCM samples not yet aligned to a 512-sample VAD frame.
    vad_remainder: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.int16)
    )
    # Leftover PCM not yet aligned to a 1280-sample wake-interpreter frame.
    wake_remainder: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.int16)
    )
    # Raw int16 PCM of the armed command turn (arm -> EOT), for batch ASR.
    capture_chunks: list = field(default_factory=list)
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
    # --- "prime + say it" positive-capture mode (data collection) ---
    priming: bool = False
    prime_deadline: float = 0.0  # monotonic; cancel if no speech heard by then
    prime_speech_started: bool = False
    prime_chunks: list = field(default_factory=list)
    prime_silence_run: int = 0  # consecutive silent VAD frames after speech began


@dataclass
class WakeEvent:
    """Emitted when a turn is captured.

    Two kinds:
      - ``command``: a real acoustic arm + captured command turn (the live Hermes
        path). ``audio`` is the command; ``trigger_audio`` is the wake-word window
        snapshotted at arm, saved for false-positive review.
      - ``primed_positive``: a "prime + say it" data-collection capture. ``audio``
        is the spoken wake-word utterance; ``score`` is the model's max score over
        it and ``is_false_negative`` is True when that fell below threshold.
    """

    client_id: str
    session_id: str
    # Raw int16 PCM @16k of the captured turn (command, or primed wake utterance).
    audio: bytes
    arm_time: float
    eot_time: float
    score: float
    reason: str  # "smart_turn" | "max_duration" | "stream_end" | "primed" | ...
    kind: str = "command"  # "command" | "primed_positive"
    # Wake-trigger window captured at arm (command kind only), for FP review.
    trigger_audio: bytes = b""
    # primed_positive only: did the live model under-score this true positive?
    is_false_negative: bool = False


class HermesDetector:
    """Loads the wake model + builds per-client capture state."""

    def __init__(
        self,
        model_path: str,
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
        prime_timeout_secs: float = 12.0,
        prime_trail_silence_secs: float = 0.6,
        prime_max_secs: float = 4.0,
    ):
        """Initialize the detector.

        Args:
            model_path: Path to the trained ``hermes.onnx`` wake-word model.
            threshold: Acoustic detection threshold (favor precision).
            patience: Require this many CONSECUTIVE frames over threshold before
                firing — the main false-positive suppressor (raw per-frame
                thresholding alone over-triggers on real ambient noise).
            debounce_secs: Suppress repeat arming within this window.
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
        self.model_path = model_path
        self.threshold = threshold
        self.patience = patience
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
        self.smart_turn_model_path = smart_turn_model_path

        logger.info(f"Loading wake-word model: {model_path} (threshold={threshold})")
        self._interpreter = NanoInterpreter.load_model(model_path)
        self._wake_key = next(iter(self._interpreter.models.keys()))
        logger.info(f"Wake-word model loaded (key='{self._wake_key}')")

        # Resolve the Silero VAD ONNX path once (explicit override or bundled).
        if silero_vad_model_path:
            self._vad_model_path = silero_vad_model_path
        else:
            import importlib.resources as ir

            self._vad_model_path = str(
                ir.files("pipecat.audio.vad.data").joinpath("silero_vad.onnx")
            )

    def new_client_state(self) -> ClientWakeState:
        """Build fresh per-client state with its own VAD + Smart Turn instances."""
        analyzer = LocalSmartTurnAnalyzerV3(
            smart_turn_model_path=self.smart_turn_model_path,
            params=SmartTurnParams(stop_secs=self.stop_secs),
        )
        analyzer.set_sample_rate(SAMPLE_RATE)
        return ClientWakeState(
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
            self._run_wake(state, client_id, audio)
            return None

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

    def _run_wake(
        self, state: ClientWakeState, client_id: str, audio: np.ndarray
    ) -> None:
        # Reframe to exactly 1280-sample frames (carry a remainder across calls);
        # the interpreter scores 0.0 on any other frame size.
        buf = (
            np.concatenate([state.wake_remainder, audio])
            if state.wake_remainder.size
            else audio
        )
        n_full = (buf.size // WAKE_FRAME_SAMPLES) * WAKE_FRAME_SAMPLES
        state.wake_remainder = buf[n_full:].copy()

        # Manual patience: require `self.patience` CONSECUTIVE 1280-frames over
        # threshold before arming. (The interpreter's own patience/threshold-dict
        # + .detected path never fires here, so we gate on the raw score, which is
        # what evaluate_hermes.py validated.) This is the main FP suppressor.
        for i in range(0, n_full, WAKE_FRAME_SAMPLES):
            score = self._interpreter.predict(buf[i : i + WAKE_FRAME_SAMPLES]).get(
                self._wake_key, 0.0
            )
            if self.score_log_floor and score > self.score_log_floor:
                logger.info(f"score {score:.3f} '{client_id}'")
            state.consec = state.consec + 1 if score > self.threshold else 0
            now = time.monotonic()
            if (
                state.consec >= self.patience
                and (now - state.last_detection_time) > self.debounce_secs
            ):
                state.armed = True
                state.arm_time = now
                state.arm_score = score
                state.last_detection_time = now
                state.consec = 0
                # Snapshot the wake-trigger window for false-positive review (the
                # audio that *caused* this arm, distinct from the command turn).
                state.trigger_audio = self._preroll_tail(
                    state, PREROLL_SAMPLES
                ).tobytes()
                # Reset interpreter + wake remainder so capture/next wake start clean.
                self._interpreter.reset()
                state.wake_remainder = np.empty(0, dtype=np.int16)
                logger.info(f"🔔 ARMED '{client_id}' (score={score:.4f})")
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
        event = WakeEvent(
            client_id=client_id,
            session_id=session_id,
            audio=captured,
            arm_time=state.arm_time,
            eot_time=eot_time,
            score=state.arm_score,
            reason=reason,
            kind="command",
            trigger_audio=state.trigger_audio,
        )
        logger.info(
            f"🛑 CAPTURED '{client_id}' reason={reason} "
            f"dur={eot_time - state.arm_time:.2f}s audio={len(captured)}B"
        )
        # Reset state for the next wake word.
        state.armed = False
        state.arm_time = 0.0
        state.arm_score = 0.0
        state.trigger_audio = b""
        state.vad_remainder = np.empty(0, dtype=np.int16)
        state.wake_remainder = np.empty(0, dtype=np.int16)
        state.capture_chunks = []
        state.eot_speech_seen = False
        state.eot_silence_frames = 0
        state.eot_next_check = 0
        if state.turn_analyzer is not None:
            state.turn_analyzer.clear()
        return event

    # ------------------------------------------------------------------ #
    # "Prime + say it" positive capture (false-negative / hard-positive collection)
    # ------------------------------------------------------------------ #

    def start_priming(self, state: ClientWakeState, client_id: str) -> None:
        """Arm a one-shot positive capture: the next utterance is a known wake word.

        Used by the data-collection UI ("I'll say the wake word now"). The next
        VAD-detected utterance is captured as a labeled positive regardless of the
        model's score; the score is computed afterwards to flag false negatives.
        """
        state.priming = True
        state.prime_deadline = time.monotonic() + self.prime_timeout_secs
        state.prime_speech_started = False
        state.prime_chunks = []
        state.prime_silence_run = 0
        state.vad_remainder = np.empty(0, dtype=np.int16)
        if state.turn_analyzer is not None:
            state.turn_analyzer.clear()
        logger.info(f"🎯 PRIMED positive capture for '{client_id}' — awaiting speech")

    def _run_prime(
        self, state: ClientWakeState, client_id: str, session_id: str, audio: np.ndarray
    ) -> Optional[WakeEvent]:
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
            is_speech = conf >= self.vad_threshold

            if not state.prime_speech_started:
                if is_speech:
                    # Seed with a short lead-in so the onset isn't clipped.
                    state.prime_speech_started = True
                    state.prime_chunks = [
                        self._preroll_tail(state, PRIME_LEADIN_SAMPLES)
                    ]
                    state.prime_chunks.append(frame)
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
        elif time.monotonic() > state.prime_deadline:
            # Heard no speech in the priming window — cancel quietly.
            logger.info(f"🎯 prime for '{client_id}' timed out (no speech)")
            self._reset_prime(state)
        return None

    def _finish_prime(
        self, state: ClientWakeState, client_id: str, session_id: str, reason: str
    ) -> WakeEvent:
        captured = (
            np.concatenate(state.prime_chunks)
            if state.prime_chunks
            else np.empty(0, dtype=np.int16)
        )
        score = self._score_buffer(captured)
        is_fn = score < self.threshold
        event = WakeEvent(
            client_id=client_id,
            session_id=session_id,
            audio=captured.tobytes(),
            arm_time=0.0,
            eot_time=time.monotonic(),
            score=score,
            reason=reason,
            kind="primed_positive",
            is_false_negative=is_fn,
        )
        logger.info(
            f"🎯 PRIMED POSITIVE '{client_id}' score={score:.4f} "
            f"{'(FALSE NEGATIVE)' if is_fn else '(would-have-fired)'} "
            f"dur={captured.size / SAMPLE_RATE:.2f}s reason={reason}"
        )
        self._reset_prime(state)
        return event

    def _score_buffer(self, audio: np.ndarray) -> float:
        """Max wake score over a buffer (reset interpreter before/after so the
        live streaming state isn't polluted)."""
        self._interpreter.reset()
        best = 0.0
        n_full = (audio.size // WAKE_FRAME_SAMPLES) * WAKE_FRAME_SAMPLES
        for i in range(0, n_full, WAKE_FRAME_SAMPLES):
            s = self._interpreter.predict(audio[i : i + WAKE_FRAME_SAMPLES]).get(
                self._wake_key, 0.0
            )
            best = max(best, s)
        self._interpreter.reset()
        return float(best)

    @staticmethod
    def _reset_prime(state: ClientWakeState) -> None:
        state.priming = False
        state.prime_deadline = 0.0
        state.prime_speech_started = False
        state.prime_chunks = []
        state.prime_silence_run = 0
        state.vad_remainder = np.empty(0, dtype=np.int16)
        if state.turn_analyzer is not None:
            state.turn_analyzer.clear()
