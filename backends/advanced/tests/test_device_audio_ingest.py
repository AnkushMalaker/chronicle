from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from advanced_omi_backend.services.device_audio_ingest import (
    audio_stream_key,
    group_audio_sessions,
)


def item(identifier: str, start: datetime, duration: float = 30):
    return SimpleNamespace(
        source_item_id=identifier,
        captured_at=start,
        ended_at=start + timedelta(seconds=duration),
    )


def test_audio_stream_key_separates_microphone_from_system_output():
    microphone = SimpleNamespace(
        user_id="user", source_id="rainbow", metadata={"direction": "input"}
    )
    system = SimpleNamespace(
        user_id="user", source_id="rainbow", metadata={"direction": "output"}
    )

    assert audio_stream_key(microphone) != audio_stream_key(system)


def test_audio_chunks_group_across_input_and_output_devices():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("input-1", start),
            item("output-1", start),
            item("input-2", start + timedelta(seconds=30)),
        ]
    )
    assert len(sessions) == 1
    assert len(sessions[0]) == 3


def test_audio_session_closes_after_meaningful_gap():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("one", start),
            item("two", start + timedelta(minutes=3)),
        ]
    )
    assert [[row.source_item_id for row in session] for session in sessions] == [
        ["one"],
        ["two"],
    ]


def test_continuous_capture_is_bounded_into_processing_windows():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [item(str(index), start + timedelta(minutes=index)) for index in range(32)]
    sessions = group_audio_sessions(rows)
    assert [len(session) for session in sessions] == [30, 2]


def test_mongo_naive_and_aware_timestamps_group_together():
    naive = datetime(2026, 7, 22, 10, 0)
    aware = datetime(2026, 7, 22, 10, 0, 30, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("mongo-naive", naive),
            item("api-aware", aware),
        ]
    )
    assert [[row.source_item_id for row in session] for session in sessions] == [
        ["mongo-naive", "api-aware"]
    ]


def test_detect_wav_speech_reads_wav_and_reports_vad_verdict(tmp_path, monkeypatch):
    import wave

    import advanced_omi_backend.utils.vad_analysis as vad_analysis
    from advanced_omi_backend.services.device_audio_ingest import _detect_wav_speech
    from advanced_omi_backend.utils.vad_analysis import SpeechDetectionReason

    path = tmp_path / "session.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)

    class FakeProvider:
        frame_hop_ms = 16

        def __init__(self, scores):
            self._scores = scores

        def score(self, mono, sample_rate):
            return self._scores

    monkeypatch.setattr(
        vad_analysis, "get_vad_provider", lambda: FakeProvider([0.9] * 40)
    )
    speech = _detect_wav_speech(path)
    assert speech.has_speech is True
    assert speech.scored is True
    assert speech.reason is SpeechDetectionReason.SPEECH_DETECTED

    monkeypatch.setattr(
        vad_analysis, "get_vad_provider", lambda: FakeProvider([0.1] * 40)
    )
    silence = _detect_wav_speech(path)
    assert silence.should_reject is True
    assert silence.reason is SpeechDetectionReason.NO_SPEECH


def test_detect_wav_speech_reports_decode_failure(tmp_path):
    from advanced_omi_backend.services.device_audio_ingest import _detect_wav_speech
    from advanced_omi_backend.utils.vad_analysis import SpeechDetectionReason

    missing = tmp_path / "missing.wav"
    result = _detect_wav_speech(missing)

    assert result.has_speech is None
    assert result.scored is False
    assert result.should_reject is False
    assert result.reason is SpeechDetectionReason.WAV_DECODE_FAILED
    assert result.detail.startswith("FileNotFoundError:")
