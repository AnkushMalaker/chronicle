"""Regression coverage for operational summary statistics."""

from datetime import datetime

import pytest

from backend.controllers import system_events_controller


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, *, length):
        assert length == 1
        return self._rows


class _FakeCollection:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.pipelines = []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return _FakeCursor(next(self._responses))


@pytest.mark.asyncio
async def test_summary_counts_each_deterministic_fallback_occurrence(monkeypatch):
    # Raw PyMongo aggregation values are UTC but may be returned without tzinfo.
    latest = datetime(2026, 8, 18, 10, 30)
    collection = _FakeCollection(
        [
            [
                {
                    "by_severity": [{"_id": "warning", "count": 4}],
                    "by_category": [{"_id": "memory", "count": 4}],
                    "by_source": [{"_id": "memory.provider.chronicle", "count": 4}],
                    "total": [{"n": 4}],
                    "unacked": [{"n": 3}],
                }
            ],
            [
                {
                    "total": [{"n": 5}],
                    "affected_conversations": [{"n": 3}],
                    "by_reason": [
                        {"_id": "incomplete_agent", "count": 4},
                        {"_id": "invalid_note", "count": 2},
                    ],
                    "by_user": [{"_id": "ankush", "count": 5}],
                    "by_primary_backend": [{"_id": "pi", "count": 5}],
                    "by_recovery_backend": [{"_id": "direct", "count": 5}],
                    "by_agent_path": [
                        {
                            "_id": {"primary": "pi", "recovery": "direct"},
                            "count": 5,
                        }
                    ],
                    "latest": [{"at": latest}],
                }
            ],
        ]
    )
    monkeypatch.setattr(
        system_events_controller.SystemEvent,
        "get_pymongo_collection",
        lambda: collection,
    )

    summary = await system_events_controller.get_system_events_summary(window_hours=24)

    assert summary["memory_fallbacks"] == {
        "occurrences": 5,
        "affected_conversations": 3,
        "by_reason": {"incomplete_agent": 4, "invalid_note": 2},
        "by_user": {"ankush": 5},
        "by_primary_backend": {"pi": 5},
        "by_recovery_backend": {"direct": 5},
        "agent_paths": [
            {
                "primary_backend": "pi",
                "recovery_backend": "direct",
                "occurrences": 5,
            }
        ],
        "latest_at": "2026-08-18T10:30:00+00:00",
    }

    fallback_pipeline = collection.pipelines[1]
    first_match = fallback_pipeline[0]["$match"]
    assert first_match["category"] == "memory"
    assert (
        first_match["metadata.fallback_type"] == "deterministic_source_preserving_note"
    )
    assert "$gte" in first_match["occurrence_times"]
    assert fallback_pipeline[1] == {"$unwind": "$occurrence_times"}
    assert "$gte" in fallback_pipeline[2]["$match"]["occurrence_times"]


@pytest.mark.asyncio
async def test_summary_returns_zeroed_fallback_stats_when_none_occurred(monkeypatch):
    collection = _FakeCollection(
        [
            [
                {
                    "by_severity": [],
                    "by_category": [],
                    "by_source": [],
                    "total": [],
                    "unacked": [],
                }
            ],
            [
                {
                    "total": [],
                    "affected_conversations": [],
                    "by_reason": [],
                    "by_user": [],
                    "by_primary_backend": [],
                    "by_recovery_backend": [],
                    "by_agent_path": [],
                    "latest": [],
                }
            ],
        ]
    )
    monkeypatch.setattr(
        system_events_controller.SystemEvent,
        "get_pymongo_collection",
        lambda: collection,
    )

    summary = await system_events_controller.get_system_events_summary(window_hours=24)

    assert summary["memory_fallbacks"] == {
        "occurrences": 0,
        "affected_conversations": 0,
        "by_reason": {},
        "by_user": {},
        "by_primary_backend": {},
        "by_recovery_backend": {},
        "agent_paths": [],
        "latest_at": None,
    }
