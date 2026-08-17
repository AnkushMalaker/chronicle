"""Regression tests for the unattended day-rebuild finisher."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory.vault_scaffold import seed_vault_scaffold
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


class TimelineRunsCollection:
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
        ),
        "timeline_analysis_runs": TimelineRunsCollection([]),
    }

    pending = await finish_day_rebuild._unwritten_days(database, "user-1", expected)

    assert [row["local_date"].day for row in pending] == [3, 4]
    assert pending[0]["state"] == "missing"
    assert pending[1]["error"] == "provider timeout"


@pytest.mark.asyncio
async def test_unwritten_days_accepts_latest_completed_empty_evidence_run():
    local_date = datetime(2026, 6, 1).date()
    expected = [{"local_date": local_date, "timezone": "Asia/Kolkata"}]
    database = {
        "timeline_days": TimelineDaysCollection([]),
        "timeline_analysis_runs": TimelineRunsCollection(
            [
                {
                    "local_date": datetime(2026, 6, 1),
                    "timezone": "Asia/Kolkata",
                    "state": "failed",
                    "created_at": datetime(2026, 8, 12, 10),
                    "completed_at": datetime(2026, 8, 12, 10, 1),
                },
                {
                    "local_date": datetime(2026, 6, 1),
                    "timezone": "Asia/Kolkata",
                    "state": "awaiting_evidence",
                    "created_at": datetime(2026, 8, 13, 10),
                    "completed_at": datetime(2026, 8, 13, 10, 1),
                },
            ]
        ),
    }

    pending = await finish_day_rebuild._unwritten_days(database, "user-1", expected)

    assert pending == []


@pytest.mark.asyncio
async def test_unwritten_days_does_not_accept_stale_empty_evidence_run():
    local_date = datetime(2026, 6, 1).date()
    expected = [{"local_date": local_date, "timezone": "Asia/Kolkata"}]
    database = {
        "timeline_days": TimelineDaysCollection([]),
        "timeline_analysis_runs": TimelineRunsCollection(
            [
                {
                    "local_date": datetime(2026, 6, 1),
                    "timezone": "Asia/Kolkata",
                    "state": "awaiting_evidence",
                    "created_at": datetime(2026, 8, 12, 10),
                    "completed_at": datetime(2026, 8, 12, 10, 1),
                },
                {
                    "local_date": datetime(2026, 6, 1),
                    "timezone": "Asia/Kolkata",
                    "state": "failed",
                    "created_at": datetime(2026, 8, 13, 10),
                    "completed_at": datetime(2026, 8, 13, 10, 1),
                },
            ]
        ),
    }

    pending = await finish_day_rebuild._unwritten_days(database, "user-1", expected)

    assert [row["local_date"] for row in pending] == [local_date]


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


def test_repair_job_id_and_metadata_are_scoped_to_the_rebuild(monkeypatch):
    captured = {}

    def fake_enqueue(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=kwargs["job_id"])

    monkeypatch.setattr(
        finish_day_rebuild,
        "memory_queue",
        SimpleNamespace(enqueue=fake_enqueue),
    )
    row = {"local_date": datetime(2026, 1, 6).date(), "timezone": "UTC"}

    job = finish_day_rebuild._enqueue(
        "user-1",
        row,
        repair_scope="run-1",
        sequence="2_3",
        depends_on=None,
    )

    assert job.id == "day_retry_run-1_2_3_2026-01-06"
    assert captured["meta"] == {
        "user_id": "user-1",
        "rebuild_run_id": "run-1",
        "local_date": "2026-01-06",
        "trigger": "timeline_rebuild_repair",
    }


def test_finisher_refuses_to_validate_an_incomplete_rebuild():
    with pytest.raises(
        RuntimeError,
        match=r"incomplete after repair budget: 2 day\(s\): 2026-07-23, 2026-08-04",
    ):
        finish_day_rebuild._require_complete(
            [
                {"local_date": datetime(2026, 7, 23).date()},
                {"local_date": datetime(2026, 8, 4).date()},
            ]
        )


def test_finisher_accepts_a_complete_rebuild():
    finish_day_rebuild._require_complete([])


def test_finisher_rejects_a_content_note_at_the_vault_root(tmp_path):
    seed_vault_scaffold(tmp_path)
    (tmp_path / "Misplaced Topic.md").write_text(
        "## About\n- This should live under Topics/.\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=r"Misplaced Topic.md \[root_note_role\]"):
        finish_day_rebuild._require_valid_vault(tmp_path)


def test_finisher_rejects_a_missing_vault_scaffold(tmp_path):
    with pytest.raises(RuntimeError, match="missing required scaffold files"):
        finish_day_rebuild._require_valid_vault(tmp_path)


def test_finisher_accepts_a_structurally_clean_vault(tmp_path):
    seed_vault_scaffold(tmp_path)
    topics = tmp_path / "Topics"
    topics.mkdir()
    (topics / "Speaker diarization.md").write_text(
        """---
categories:
  - "[[Topics]]"
---
## About
- Speaker diarization notes.

## Conversations
![[Conversations.base#Topic]]
""",
        encoding="utf-8",
    )

    assert finish_day_rebuild._require_valid_vault(tmp_path) == 7
