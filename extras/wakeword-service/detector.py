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
    arm_time: float = 0.0
    arm_score: float = 0.0
    last_detection_time: float = 0.0
    # Consecutive 1280-frames whose raw wake score exceeds threshold (patience).
    consec: int = 0
    # Smart Turn analyzer is per-client (it holds an audio buffer + thread).
    turn_analyzer: Optional[LocalSmartTurnAnalyzerV3] = None
    vad_model: Optional[SileroOnnxModel] = None
    # Per-client wake interpreter. NanoInterpreter holds a STREAMING feature
    # buffer (raw audio -> mel -> 96-d embeddings) that spans ~2 s of context, so
    # it must not be shared across clients/streams — a shared one interleaves
    # unrelated audio and produces spurious scores.
    interpreter: Optional["NanoInterpreter"] = None
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
    # Dev capture: interpreter buffer state snapshotted at arm. The model's ~2 s
    # receptive field exceeds the 1.5 s trigger_audio window, so a cold replay of
    # trigger_audio alone often won't reproduce the live score. These make an arm
    # exactly reproducible offline: trigger_features = the (N, 96) embedding buffer
    # the wake model scored on; trigger_context = the full ~10 s raw-audio buffer.
    trigger_features: Optional[np.ndarray] = None
    trigger_context: bytes = b""
    # --- "prime + say it" positive-capture mode (data collection) ---
    priming: bool = False
    prime_start: float = 0.0  # monotonic; whole-session hard-cap reference
    prime_stop_requested: bool = False  # manual "end now" from the UI
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
        prime_timeout_secs: float = 10.0,
        prime_trail_silence_secs: float = 0.6,
        prime_max_secs: float = 4.0,
        prime_vad_threshold: float = 0.3,
        min_command_speech_secs: float = 0.3,
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
        # throwaway probe here only to validate the model and read the wake key.
        logger.info(f"Loading wake-word model: {model_path} (threshold={threshold})")
        probe = NanoInterpreter.load_model(model_path)
        self._wake_key = next(iter(probe.models.keys()))
        del probe
        logger.info(
            f"Wake-word model loaded (key='{self._wake_key}', per-client interpreters)"
        )

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
            interpreter=NanoInterpreter.load_model(self.model_path),
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
            score = state.interpreter.predict(buf[i : i + WAKE_FRAME_SAMPLES]).get(
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
                # Dev: snapshot the interpreter's streaming buffer state that
                # produced this arm BEFORE the reset clears it — makes the arm
                # exactly reproducible offline (the model's ~2 s receptive field
                # exceeds the 1.5 s trigger_audio window).
                state.trigger_features, state.trigger_context = self._snapshot_buffers(
                    state.interpreter
                )
                # Reset interpreter + wake remainder so capture/next wake start clean.
                state.interpreter.reset()
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
            audio=captured,
            arm_time=state.arm_time,
            eot_time=eot_time,
            score=state.arm_score,
            reason=reason,
            kind="command",
            has_speech=has_speech,
            trigger_audio=state.trigger_audio,
            buffer_features=state.trigger_features,
            buffer_context=state.trigger_context,
        )
        logger.info(
            f"🛑 CAPTURED '{client_id}' reason={reason} "
            f"dur={eot_time - state.arm_time:.2f}s audio={len(captured)}B "
            f"speech={speech_secs:.2f}s has_speech={has_speech}"
        )
        # Reset state for the next wake word.
        state.armed = False
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

    def start_priming(self, state: ClientWakeState, client_id: str) -> None:
        """Arm a one-shot positive capture: the next utterance is a known wake word.

        Used by the data-collection UI ("I'll say the wake word now"). The next
        VAD-detected utterance is captured as a labeled positive regardless of the
        model's score; the score is computed afterwards to flag false negatives.
        """
        state.priming = True
        state.prime_start = time.monotonic()
        state.prime_stop_requested = False
        state.prime_speech_started = False
        state.prime_chunks = []
        state.prime_silence_run = 0
        state.vad_remainder = np.empty(0, dtype=np.int16)
        if state.turn_analyzer is not None:
            state.turn_analyzer.clear()
        logger.info(f"🎯 PRIMED positive capture for '{client_id}' — awaiting speech")

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
        if state.prime_chunks:
            captured = np.concatenate(state.prime_chunks)
        else:
            # Manual stop / timeout with no VAD-gated speech: fall back to the
            # recent pre-roll so the attempt still surfaces for review instead of
            # vanishing (the user explicitly asked for it to always show up).
            captured = self._preroll_tail(state, PREROLL_SAMPLES)
        score = (
            self._score_buffer(state.interpreter, captured) if captured.size else 0.0
        )
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

    def _score_buffer(self, interp, audio: np.ndarray) -> float:
        """Max wake score over a buffer using this client's interpreter (reset
        before/after so the live streaming state isn't polluted)."""
        interp.reset()
        best = 0.0
        n_full = (audio.size // WAKE_FRAME_SAMPLES) * WAKE_FRAME_SAMPLES
        for i in range(0, n_full, WAKE_FRAME_SAMPLES):
            s = interp.predict(audio[i : i + WAKE_FRAME_SAMPLES]).get(
                self._wake_key, 0.0
            )
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
        state.prime_start = 0.0
        state.prime_stop_requested = False
        state.prime_speech_started = False
        state.prime_chunks = []
        state.prime_silence_run = 0
        state.vad_remainder = np.empty(0, dtype=np.int16)
        if state.turn_analyzer is not None:
            state.turn_analyzer.clear()
