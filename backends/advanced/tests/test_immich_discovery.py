from datetime import datetime, timedelta, timezone

from advanced_omi_backend.services.immich_discovery import select_candidates


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
