from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from advanced_omi_backend.services.timeline.contracts import TimelineEvidenceItem
from advanced_omi_backend.services.timeline.evidence import (
    _audio_item,
    _clip_evidence_to_range,
    _coalesce_application_evidence,
    _device_items,
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


def _app_item(
    evidence_id: str,
    source_id: str,
    started_at: datetime,
    *,
    browser_url: str = "",
) -> TimelineEvidenceItem:
    return TimelineEvidenceItem(
        evidence_id=evidence_id,
        kind="observation",
        source_id=source_id,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=10),
        role="application_state",
        excerpt=evidence_id,
        metadata={
            "source_kind": "screen_context",
            "app_name": "Editor",
            "window_name": "Project",
            "browser_url": browser_url,
        },
    )


def test_reverse_order_does_not_merge_distant_screen_evidence():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    rows = [
        _app_item("later", "laptop", start + timedelta(hours=2)),
        _app_item("earlier", "laptop", start),
    ]

    compacted = _coalesce_application_evidence(rows)

    assert [item.evidence_id for item in compacted] == ["earlier", "later"]


def test_interleaved_sources_are_compacted_independently_then_merged_for_user():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    rows = [
        _app_item("desktop-1", "desktop", start),
        _app_item("laptop-1", "laptop", start + timedelta(seconds=10)),
        _app_item("desktop-2", "desktop", start + timedelta(seconds=30)),
        _app_item("laptop-2", "laptop", start + timedelta(seconds=40)),
    ]

    compacted = _coalesce_application_evidence(rows)

    assert [
        (item.source_id, item.metadata["coalesced_count"]) for item in compacted
    ] == [
        ("desktop", 2),
        ("laptop", 2),
    ]
    assert compacted[0].started_at == start
    assert compacted[0].ended_at == start + timedelta(seconds=40)
    assert compacted[1].started_at == start + timedelta(seconds=10)
    assert compacted[1].ended_at == start + timedelta(seconds=50)


def test_same_window_on_different_browser_pages_is_not_compacted():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    rows = [
        _app_item("one", "laptop", start, browser_url="https://one.example"),
        _app_item(
            "two",
            "laptop",
            start + timedelta(seconds=20),
            browser_url="https://two.example",
        ),
    ]

    assert len(_coalesce_application_evidence(rows)) == 2


def test_stale_observation_is_split_at_unsupported_liveness_gap():
    start = datetime(2026, 8, 5, 10, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    row = SimpleNamespace(
        id="row-one",
        user_id="user",
        source_id="laptop",
        kind="observation",
        source_item_id="observation:one",
        captured_at=start,
        ended_at=end,
        metadata={
            "app_name": "Editor",
            "window_name": "Project",
            "preview_frame_id": 2,
        },
        samples=[
            {"captured_at": start.isoformat(), "liveness": False, "text": "first"},
            {"captured_at": end.isoformat(), "liveness": True, "text": "second"},
        ],
        frame_candidates=[
            {"captured_at": start.isoformat(), "frame_id": 1},
            {"captured_at": end.isoformat(), "frame_id": 2},
        ],
        content_hash=None,
        curation_revision=None,
        media_data=b"preview",
        curation=None,
        media_content_type="image/jpeg",
        lifecycle="closed",
    )

    items = _device_items(row)

    assert len(items) == 2
    assert [item.started_at for item in items] == [start, end]
    assert all(item.ended_at is None for item in items)
    assert "first" in (items[0].excerpt or "")
    assert "second" not in (items[0].excerpt or "")
    assert "second" in (items[1].excerpt or "")
    assert "first" not in (items[1].excerpt or "")
    assert [item.image_filename is not None for item in items] == [False, True]


def test_audio_from_multiple_sources_remains_separate():
    started_at = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(
            id="audio-a",
            source_id="microphone-a",
            first_source_item_id="chunk-a",
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=10),
            direction="input",
            source_range_hash="hash-a",
            meeting_id=None,
            state="transcribed",
            covered_seconds=600,
            missing_seconds=0,
            bucket_seconds=10,
            coverage_fraction=[1.0],
            speech_fraction=[0.5],
            acoustic_active_fraction=[0.7],
            rms_dbfs=[-20.0],
            peak_dbfs=[-5.0],
            longest_no_speech_seconds=20,
            conversation_id=None,
        ),
        SimpleNamespace(
            id="audio-b",
            source_id="microphone-b",
            first_source_item_id="chunk-b",
            started_at=started_at + timedelta(minutes=2),
            ended_at=started_at + timedelta(minutes=8),
            direction="input",
            source_range_hash="hash-b",
            meeting_id=None,
            state="transcribed",
            covered_seconds=360,
            missing_seconds=0,
            bucket_seconds=10,
            coverage_fraction=[1.0],
            speech_fraction=[0.5],
            acoustic_active_fraction=[0.7],
            rms_dbfs=[-20.0],
            peak_dbfs=[-5.0],
            longest_no_speech_seconds=20,
            conversation_id=None,
        ),
    ]

    evidence = [_audio_item(row) for row in rows]

    assert [item.evidence_id for item in evidence] == [
        "audio_span:audio-a",
        "audio_span:audio-b",
    ]
    assert [item.source_id for item in evidence] == ["microphone-a", "microphone-b"]


def test_evidence_is_clipped_to_the_requested_local_day():
    day_start = datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    item = _app_item("crossing", "laptop", day_start - timedelta(hours=1))
    item.ended_at = day_start + timedelta(hours=2)

    clipped = _clip_evidence_to_range([item], day_start, day_end)

    assert len(clipped) == 1
    assert clipped[0].started_at == day_start
    assert clipped[0].ended_at == day_start + timedelta(hours=2)
