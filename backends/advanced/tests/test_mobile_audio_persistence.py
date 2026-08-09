from datetime import datetime, timezone

from advanced_omi_backend.workers.audio_jobs import _captured_at_from_fields


def test_mobile_capture_time_takes_precedence_over_redis_arrival_time():
    captured = datetime(2026, 8, 9, 12, 34, 56, tzinfo=timezone.utc)
    fields = {b"captured_at": str(captured.timestamp()).encode()}

    assert _captured_at_from_fields(fields, "1999999999000-0") == captured


def test_invalid_mobile_capture_time_falls_back_to_redis_stream_time():
    stream_id = "1786278896000-0"

    assert _captured_at_from_fields(
        {b"captured_at": b"invalid"}, stream_id
    ) == datetime.fromtimestamp(1786278896, tz=timezone.utc)
