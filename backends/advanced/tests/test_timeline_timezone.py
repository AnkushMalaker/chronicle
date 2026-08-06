from zoneinfo import ZoneInfoNotFoundError

from advanced_omi_backend.services.timeline import timezone
from advanced_omi_backend.services.timeline.timezone import canonical_timezone


def test_legacy_browser_timezone_alias_is_canonicalized():
    assert canonical_timezone("Asia/Calcutta") == "Asia/Kolkata"


def test_alias_is_mapped_before_zone_file_validation(monkeypatch):
    monkeypatch.setattr(
        timezone, "_timezone_aliases", lambda: {"Legacy/Browser": "UTC"}
    )

    def zone_info(value: str):
        if value == "Legacy/Browser":
            raise ZoneInfoNotFoundError(value)
        assert value == "UTC"
        return object()

    monkeypatch.setattr(timezone, "ZoneInfo", zone_info)

    assert canonical_timezone("Legacy/Browser") == "UTC"
