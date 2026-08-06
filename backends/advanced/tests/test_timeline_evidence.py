from datetime import date, datetime, timezone

from advanced_omi_backend.services.timeline.contracts import TimelineEvidenceItem
from advanced_omi_backend.services.timeline.evidence import (
    _coalesce_application_evidence,
    day_bounds,
)


def test_local_day_bounds_respect_non_utc_timezone():
    start, end = day_bounds(date(2026, 8, 6), "Asia/Kolkata")

    assert start == datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 6, 18, 30, tzinfo=timezone.utc)


def test_dst_day_uses_real_local_midnights():
    start, end = day_bounds(date(2026, 11, 1), "America/New_York")

    assert (end - start).total_seconds() == 25 * 60 * 60


def test_adjacent_app_rows_are_compacted_without_becoming_episode_boundaries():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    rows = [
        TimelineEvidenceItem(
            evidence_id=f"observation:{index}",
            kind="observation",
            source_id="rainbow",
            started_at=start,
            ended_at=start,
            role="application_state",
            excerpt=f"state {index}",
            metadata={
                "source_kind": "activity",
                "app_name": "VLC",
                "window_name": "Terminator",
            },
        )
        for index in range(2)
    ]

    compacted = _coalesce_application_evidence(rows)

    assert len(compacted) == 1
    assert compacted[0].metadata["coalesced_count"] == 2
    assert "state 0" in compacted[0].excerpt
    assert "state 1" in compacted[0].excerpt
