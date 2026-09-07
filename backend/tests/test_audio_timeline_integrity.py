from datetime import datetime, timedelta, timezone

from backend.services.audio_timeline_integrity import plan_contiguous_chunk_timeline


def test_rebases_chunks_and_quarantines_same_capture_time_duplicates():
    captured = datetime(2026, 2, 15, tzinfo=timezone.utc)
    chunks = [
        {
            "_id": "short-retry",
            "chunk_index": 4,
            "captured_at": captured,
            "duration": 3.25,
        },
        {
            "_id": "full",
            "chunk_index": 4,
            "captured_at": captured,
            "duration": 10.0,
        },
        {
            "_id": "next",
            "chunk_index": 9,
            "captured_at": captured + timedelta(seconds=10),
            "duration": 8.0,
        },
    ]

    plan = plan_contiguous_chunk_timeline(chunks)

    assert plan.duplicate_ids == ["short-retry"]
    assert plan.duration == 18.0
    assert [
        (item.document_id, item.chunk_index, item.start_time, item.end_time)
        for item in plan.updates
    ] == [
        ("full", 0, 0.0, 10.0),
        ("next", 1, 10.0, 18.0),
    ]
