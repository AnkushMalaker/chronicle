"""Regression tests for the unattended day-rebuild finisher."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from scripts import finish_day_rebuild


class AsyncCursor:
    def __init__(self, documents):
        self.documents = iter(documents)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.documents)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class TimelineDaysCollection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query, projection=None):
        assert query == {"user_id": "user-1"}
        assert projection is not None
        return AsyncCursor(self.documents)


@pytest.mark.asyncio
async def test_unwritten_days_accepts_no_changes_and_finds_missing_days():
    expected = [
        {"local_date": datetime(2026, 1, day).date(), "timezone": "UTC"}
        for day in range(1, 5)
    ]
    database = {
        "timeline_days": TimelineDaysCollection(
            [
                {
                    "local_date": datetime(2026, 1, 1),
                    "timezone": "UTC",
                    "memory_state": "written",
                },
                {
                    "local_date": datetime(2026, 1, 2),
                    "timezone": "UTC",
                    "memory_state": "no_changes",
                },
                {
                    "local_date": datetime(2026, 1, 4),
                    "timezone": "UTC",
                    "memory_state": "",
                    "memory_attempts": 1,
                    "memory_error": "provider timeout",
                },
            ]
        )
    }

    pending = await finish_day_rebuild._unwritten_days(database, "user-1", expected)

    assert [row["local_date"].day for row in pending] == [3, 4]
    assert pending[0]["state"] == "missing"
    assert pending[1]["error"] == "provider timeout"


def test_rebuild_job_states_ignores_rq_companion_keys(monkeypatch):
    class FakeRedis:
        def scan_iter(self, match):
            assert match == "rq:job:timeline_rebuild_run-1_*"
            return iter((b"job-one", b"job-one:dependents", b"job-two"))

        def type(self, key):
            return b"set" if key.endswith(b":dependents") else b"hash"

        def hget(self, key, field):
            assert field == "status"
            return b"finished" if key == b"job-one" else b"deferred"

    monkeypatch.setattr(
        finish_day_rebuild,
        "memory_queue",
        SimpleNamespace(connection=FakeRedis()),
    )

    states = finish_day_rebuild._rebuild_job_states("run-1")

    assert states == {"finished": 1, "deferred": 1}
