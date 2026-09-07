import numpy as np

from backend.utils import vad_analysis
from backend.utils.vad_analysis import (
    SpeechDetectionReason,
    VadFrameScores,
    VadScoringError,
    profile_pcm_audio,
)


def _pcm_tone(seconds: float, amplitude: float, sample_rate: int = 16000) -> bytes:
    samples = np.arange(int(seconds * sample_rate))
    signal = amplitude * np.sin(2 * np.pi * 440 * samples / sample_rate)
    return signal.astype(np.int16).tobytes()


def test_non_speech_media_remains_acoustically_active(monkeypatch):
    monkeypatch.setattr(
        vad_analysis,
        "score_pcm_frames",
        lambda *_: VadFrameScores(
            scores=np.zeros(200, dtype=np.float32),
            hop_seconds=0.1,
            provider="fixture-vad",
        ),
    )

    profile = profile_pcm_audio(_pcm_tone(20, 8000), 16000, 1, 2)

    assert profile.scored is True
    assert profile.reason == SpeechDetectionReason.NO_SPEECH
    assert profile.speech_seconds == 0
    assert profile.acoustic_active_seconds > 19
    assert profile.acoustic_active_fraction == [1.0, 1.0]


def test_vad_failure_keeps_energy_measurements(monkeypatch):
    def fail(*_):
        raise VadScoringError(
            SpeechDetectionReason.PROVIDER_UNAVAILABLE, "fixture unavailable"
        )

    monkeypatch.setattr(vad_analysis, "score_pcm_frames", fail)

    profile = profile_pcm_audio(_pcm_tone(10, 8000), 16000, 1, 2)

    assert profile.scored is False
    assert profile.speech_fraction == [None]
    assert profile.acoustic_active_fraction == [1.0]
    assert profile.rms_dbfs[0] is not None


def test_true_quiet_is_distinct_from_missing_capture(monkeypatch):
    monkeypatch.setattr(
        vad_analysis,
        "score_pcm_frames",
        lambda *_: VadFrameScores(
            scores=np.zeros(100, dtype=np.float32), hop_seconds=0.1
        ),
    )

    profile = profile_pcm_audio(bytes(16000 * 2 * 10), 16000, 1, 2)

    assert profile.acoustic_active_seconds == 0
    assert profile.acoustic_quiet_seconds == 10
    assert profile.rms_dbfs == [None]
