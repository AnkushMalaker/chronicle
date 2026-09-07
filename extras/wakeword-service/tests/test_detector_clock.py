import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from detector import ClientWakeState, EndOfTurnState, HermesDetector
from identities import AudioChunkRef, AudioSessionRef, ClientId, SessionId


class FakeInterpreter:
    models = {"wake": object()}
    preprocessor = type(
        "Preprocessor",
        (),
        {
            "feature_buffer": np.zeros((16, 96), dtype=np.float32),
            "raw_data_buffer": np.zeros(1600, dtype=np.int16),
        },
    )()

    def predict(self, frame):
        assert len(frame) == 1280
        return {"wake": 0.97}

    def reset(self):
        pass


class PassingVerifier:
    threshold = 0.8

    def verify(self, features, wake_session, in_name):
        assert features.shape == (16, 96)
        assert wake_session is FakeInterpreter.models["wake"]
        assert in_name == "input"
        return True, 0.91


def test_arm_uses_audio_frame_end_on_the_capture_clock():
    detector = object.__new__(HermesDetector)
    detector.wakewords = ["hermes"]
    detector._wake_keys = {"hermes": "wake"}
    detector._wake_in_names = {"hermes": "input"}
    detector.thresholds = {"hermes": 0.9}
    detector.patiences = {"hermes": 1}
    detector.disabled = set()
    detector.collect_only = set()
    detector.verifiers = {"hermes": PassingVerifier()}
    detector.verifiers_disabled = set()
    detector.debounce_secs = 0
    detector.score_log_floor = 0

    state = ClientWakeState(
        interpreters={"hermes": FakeInterpreter()},
        consec={"hermes": 0},
    )
    session = AudioSessionRef(
        session_id=SessionId.from_value("session-1"),
        client_id=ClientId.from_value("user-phone"),
        capture_epoch=7,
        started_at=1_770_000_000.0,
    )
    chunk = AudioChunkRef(
        captured_at=1_770_000_001.25,
        time_basis="captured",
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )

    detector._push_preroll(state, np.zeros(1280, dtype=np.int16))
    result = detector._run_wake(state, session, chunk, np.zeros(1280, dtype=np.int16))

    assert result is None
    assert state.armed is True
    assert state.arm_occurred_at == pytest.approx(1_770_000_001.33)
    assert state.arm_offset_ms == pytest.approx(1330.0)
    assert state.arm_capture_epoch == 7
    assert state.wake_trace_id is not None
    assert state.arm_verifier_passed is True
    assert state.arm_verifier_score == pytest.approx(0.91)


def test_probe_runs_collect_only_word_through_production_verifier_path():
    detector = object.__new__(HermesDetector)
    detector.wakewords = ["hermes"]
    detector._wake_keys = {"hermes": "wake"}
    detector._wake_in_names = {"hermes": "input"}
    detector.thresholds = {"hermes": 0.9}
    detector.patiences = {"hermes": 1}
    detector.disabled = set()
    detector.collect_only = {"hermes"}
    detector.verifiers = {"hermes": PassingVerifier()}
    detector.verifiers_disabled = set()
    detector.debounce_secs = 0
    detector.score_log_floor = 0
    state = ClientWakeState(
        interpreters={"hermes": FakeInterpreter()},
        consec={"hermes": 0},
    )
    session = AudioSessionRef(
        session_id=SessionId.from_value("session-probe"),
        client_id=ClientId.from_value("user-probe"),
        capture_epoch=9,
        started_at=1_770_000_000.0,
    )
    chunk = AudioChunkRef(
        captured_at=1_770_000_001.0,
        time_basis="captured",
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )

    result = detector._run_wake(
        state,
        session,
        chunk,
        np.zeros(1280, dtype=np.int16),
        probe_wakeword="hermes",
    )

    assert result is None
    assert state.armed_wakeword == "hermes"
    assert state.arm_verifier_passed is True
    assert state.arm_verifier_score == pytest.approx(0.91)


class SpeechVad:
    def __call__(self, frame, sample_rate):
        return np.asarray([1.0])


class CompletingTurnAnalyzer:
    def append_audio(self, frame, is_speech):
        assert is_speech is True
        return EndOfTurnState.COMPLETE

    def clear(self):
        pass


@pytest.mark.asyncio
async def test_completed_command_event_carries_exact_trace_and_audio_intervals():
    detector = object.__new__(HermesDetector)
    detector.vad_threshold = 0.5
    detector.min_command_speech_frames = 1
    detector.max_arm_secs = 30
    state = ClientWakeState(
        armed=True,
        armed_wakeword="hermes",
        arm_time=10.0,
        arm_score=0.97,
        wake_trace_id="7ce4d46b-232f-47f9-8148-d595ed344cf2",
        arm_occurred_at=1_770_000_001.33,
        arm_offset_ms=1330.0,
        arm_capture_epoch=7,
        trigger_audio=b"\x00\x00" * 16000,
        turn_analyzer=CompletingTurnAnalyzer(),
        vad_model=SpeechVad(),
    )
    session = AudioSessionRef(
        session_id=SessionId.from_value("session-1"),
        client_id=ClientId.from_value("user-phone"),
        capture_epoch=7,
        started_at=1_770_000_000.0,
    )
    chunk = AudioChunkRef(
        captured_at=1_770_000_001.33,
        time_basis="captured",
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )

    event = await detector.process_frame(
        state, session, chunk, np.zeros(512, dtype=np.int16).tobytes()
    )

    assert event.wake_trace_id == "7ce4d46b-232f-47f9-8148-d595ed344cf2"
    assert event.capture_epoch == 7
    assert event.armed_at == pytest.approx(1_770_000_001.33)
    assert event.end_of_turn_at == pytest.approx(1_770_000_001.362)
    assert event.trigger_interval.start_ms == pytest.approx(330.0)
    assert event.trigger_interval.end_ms == pytest.approx(1330.0)
    assert event.command_interval.start_ms == pytest.approx(1330.0)
    assert event.command_interval.end_ms == pytest.approx(1362.0)
