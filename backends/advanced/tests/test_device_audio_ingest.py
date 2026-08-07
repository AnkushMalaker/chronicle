import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

import advanced_omi_backend.utils.vad_analysis as vad_analysis
from advanced_omi_backend.services.device_audio_ingest import (
    _profile_wav,
    audio_stream_key,
    group_audio_sessions,
)
from advanced_omi_backend.utils.vad_analysis import SpeechDetectionReason


def item(
    identifier: str,
    start: datetime,
    duration: float = 30,
    meeting_id: str | None = None,
):
    return SimpleNamespace(
        source_item_id=identifier,
        captured_at=start,
        ended_at=start + timedelta(seconds=duration),
        metadata={"meeting_id": meeting_id} if meeting_id else {},
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


def test_meeting_chunks_are_not_split_by_the_thirty_minute_window():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [
        item(str(index), start + timedelta(minutes=index), meeting_id="meeting:1")
        for index in range(45)
    ]
    sessions = group_audio_sessions(rows)
    assert [len(session) for session in sessions] == [45]


def test_meeting_boundary_starts_a_new_session():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("ambient", start),
            item("call-1", start + timedelta(seconds=30), meeting_id="meeting:1"),
            item("call-2", start + timedelta(seconds=60), meeting_id="meeting:1"),
            item("after", start + timedelta(seconds=90)),
        ]
    )
    assert [[row.source_item_id for row in session] for session in sessions] == [
        ["ambient"],
        ["call-1", "call-2"],
        ["after"],
    ]


def test_back_to_back_meetings_stay_separate_conversations():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("first", start, meeting_id="meeting:1"),
            item("second", start + timedelta(seconds=30), meeting_id="meeting:2"),
        ]
    )
    assert len(sessions) == 2


def test_meeting_tolerates_longer_silences_than_ambient_capture():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("one", start, meeting_id="meeting:1"),
            item("two", start + timedelta(minutes=4), meeting_id="meeting:1"),
            item("three", start + timedelta(minutes=11), meeting_id="meeting:1"),
        ]
    )
    assert [[row.source_item_id for row in session] for session in sessions] == [
        ["one", "two"],
        ["three"],
    ]


def test_meetings_still_split_at_the_safety_cap():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [
        item(str(index), start + timedelta(minutes=4 * index), meeting_id="meeting:1")
        for index in range(32)
    ]
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


def test_profile_wav_reads_wav_and_reports_vad_verdict(tmp_path, monkeypatch):
    path = tmp_path / "session.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)

    class FakeProvider:
        frame_hop_ms = 16
        name = "fixture-vad"

        def __init__(self, scores):
            self._scores = scores

        def score(self, mono, sample_rate):
            return self._scores

    monkeypatch.setattr(
        vad_analysis,
        "get_vad_provider",
        lambda: FakeProvider(np.array([0.9] * 40)),
    )
    speech = _profile_wav(path)
    assert speech.scored is True
    assert speech.reason is SpeechDetectionReason.SPEECH_DETECTED

    monkeypatch.setattr(
        vad_analysis,
        "get_vad_provider",
        lambda: FakeProvider(np.array([0.1] * 40)),
    )
    silence = _profile_wav(path)
    assert silence.reason is SpeechDetectionReason.NO_SPEECH


def test_profile_wav_reports_decode_failure(tmp_path):
    missing = tmp_path / "missing.wav"
    result = _profile_wav(missing)

    assert result.scored is False
    assert result.reason is SpeechDetectionReason.WAV_DECODE_FAILED
