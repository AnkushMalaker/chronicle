from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from advanced_omi_backend.routers.modules.device_input_routes import (
    _effective_source_status,
    _utc_iso,
)


def test_utc_iso_marks_naive_mongo_datetimes_as_utc():
    assert _utc_iso(datetime(2026, 7, 24, 16, 0, 23, 834000)) == (
        "2026-07-24T16:00:23.834000Z"
    )


def test_utc_iso_converts_aware_datetimes_to_utc():
    india = timezone(timedelta(hours=5, minutes=30))
    assert _utc_iso(datetime(2026, 7, 24, 21, 30, tzinfo=india)) == (
        "2026-07-24T16:00:00Z"
    )


def test_online_source_becomes_offline_when_heartbeat_is_stale():
    now = datetime(2026, 7, 24, 16, 5, tzinfo=timezone.utc)
    source = SimpleNamespace(
        provider="screenpipe",
        status="online",
        last_seen_at=datetime(2026, 7, 24, 16, 2, tzinfo=timezone.utc),
    )

    assert _effective_source_status(source, now) == "offline"


def test_recent_source_remains_online():
    now = datetime(2026, 7, 24, 16, 5, tzinfo=timezone.utc)
    source = SimpleNamespace(
        provider="screenpipe",
        status="online",
        last_seen_at=datetime(2026, 7, 24, 16, 4, 30, tzinfo=timezone.utc),
    )

    assert _effective_source_status(source, now) == "online"


def test_immich_source_uses_last_seen_as_sync_time_not_heartbeat():
    now = datetime(2026, 7, 31, 16, 5, tzinfo=timezone.utc)
    source = SimpleNamespace(
        provider="immich",
        status="online",
        last_seen_at=datetime(2026, 7, 24, 16, 2, tzinfo=timezone.utc),
    )

    assert _effective_source_status(source, now) == "online"


def test_immich_source_preserves_explicit_error_status():
    source = SimpleNamespace(
        provider="immich",
        status="error",
        last_seen_at=datetime(2026, 7, 24, 16, 2, tzinfo=timezone.utc),
    )

    assert _effective_source_status(source) == "error"
