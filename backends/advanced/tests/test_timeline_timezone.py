from advanced_omi_backend.services.timeline.timezone import canonical_timezone


def test_legacy_browser_timezone_alias_is_canonicalized():
    assert canonical_timezone("Asia/Calcutta") == "Asia/Kolkata"
