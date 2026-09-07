from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services import job_progress


@pytest.mark.asyncio
async def test_units_retries_and_events_share_one_job_snapshot(monkeypatch):
    job = SimpleNamespace(
        id="job", meta={"reconciliation_request_id": "request"}, save_meta=Mock()
    )
    events = Mock()
    monkeypatch.setattr(job_progress, "get_current_job", lambda: job)
    monkeypatch.setattr(job_progress, "publish_sse_event", events)
    await job_progress.report_job_progress(
        "context", "Block 2", completed=1, total=7, unit="blocks", user_id="owner"
    )
    await job_progress.report_job_progress("context", "Retrying block 2", attempt=2)
    progress = job.meta["progress"]
    current = next(s for s in progress["stages"] if s["id"] == "context")
    assert current["completed"] == 1
    assert current["total"] == 7
    assert current["attempt"] == 2
    assert len(progress["events"]) == 2
    assert events.call_args.args[0:2] == ("owner", "job.progress")
    assert events.call_args.args[2]["job_id"] == "job"
    await job_progress.report_job_progress("photos", "New attempt", reset=True)
    assert (
        next(s for s in progress["stages"] if s["id"] == "context")["state"]
        == "waiting"
    )
    assert len(progress["events"]) == 3


@pytest.mark.asyncio
async def test_progress_failure_does_not_fail_work(monkeypatch):
    job = SimpleNamespace(
        id="job",
        meta={"reconciliation_request_id": "r"},
        save_meta=Mock(side_effect=RuntimeError("redis unavailable")),
    )
    monkeypatch.setattr(job_progress, "get_current_job", lambda: job)
    await job_progress.report_job_progress("evidence", "Loading evidence")


@pytest.mark.asyncio
async def test_unrelated_jobs_do_not_receive_timeline_stages(monkeypatch):
    job = SimpleNamespace(id="job", meta={}, save_meta=Mock())
    monkeypatch.setattr(job_progress, "get_current_job", lambda: job)
    await job_progress.report_job_progress("photos", "Loading")
    job.save_meta.assert_not_called()
