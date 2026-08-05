from datetime import datetime, timedelta, timezone

from advanced_omi_backend.services.immich_discovery import _as_utc, select_candidates


def asset(identifier: str, when: datetime, name: str = "photo.jpg"):
    return {
        "id": identifier,
        "type": "IMAGE",
        "localDateTime": when.isoformat(),
        "originalFileName": name,
    }


def test_candidate_selection_excludes_screenshots_and_collapses_bursts():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    result = select_candidates(
        [
            asset("one", start),
            asset("burst", start + timedelta(minutes=2)),
            asset("screen", start + timedelta(hours=1), "Screenshot_1.png"),
            asset("two", start + timedelta(hours=2)),
        ]
    )
    assert [row["id"] for row in result] == ["one", "two"]


def test_candidate_selection_honors_analysis_budget():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [asset(str(i), start + timedelta(hours=i)) for i in range(20)]
    assert len(select_candidates(rows)) == 12


def test_mongo_datetime_is_restored_to_utc_for_immich_search():
    stored = datetime(2026, 7, 27, 16, 46, 7)

    assert _as_utc(stored).isoformat() == "2026-07-27T16:46:07+00:00"
