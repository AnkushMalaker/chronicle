from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from advanced_omi_backend.services.device_audio_ingest import group_audio_sessions


def item(identifier: str, start: datetime, duration: float = 30):
    return SimpleNamespace(
        source_item_id=identifier,
        captured_at=start,
        ended_at=start + timedelta(seconds=duration),
    )


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
