import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.controllers import queue_controller
from advanced_omi_backend.models.timeline import (
    DirtyEvidenceRange,
    ImmichVisualPreparationStatus,
    TimelineReconciliationRequest,
)
from advanced_omi_backend.services.immich_discovery import ImmichDayReadiness
from advanced_omi_backend.services.timeline import explicit_reconciliation
from advanced_omi_backend.services.timeline.contracts import (
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)
from advanced_omi_backend.services.timeline.immich_visual_evidence import (
    ImmichVisualPreparation,
)
from advanced_omi_backend.workers import timeline_jobs


@pytest.fixture
async def requests_db(mongo_service, monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_explicit_reconciliation_db"]
    await init_beanie(
        database=database,
        document_models=[TimelineReconciliationRequest, DirtyEvidenceRange],
    )
    await TimelineReconciliationRequest.delete_all()
    monkeypatch.setattr(
        explicit_reconciliation,
        "resolve_immich_user_id",
        lambda: _async_value("user"),
    )

    @asynccontextmanager
    async def unlocked(*_args, **_kwargs):
        yield

    monkeypatch.setattr(explicit_reconciliation, "distributed_lock", unlocked)
    yield
    await client.drop_database("test_explicit_reconciliation_db")
    client.close()


def readiness(*, ready: bool, reason: str, assets=None):
    return ImmichDayReadiness(
        ready=ready,
        reason=reason,
        target_asset_count=len(assets or []),
        latest_asset_local_date=None,
        checked_at=datetime.now(timezone.utc),
        target_assets=assets or [],
    )


def test_explicit_enqueue_replaces_ended_scheduled_retry(monkeypatch):
    deleted = []
    existing = SimpleNamespace(
        id="timeline-explicit_request-one",
        ended_at=datetime.now(timezone.utc),
        delete=lambda: deleted.append(True),
    )
    monkeypatch.setattr(
        queue_controller.Job,
        "fetch",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        queue_controller, "get_job_status_from_rq", lambda _job: "scheduled"
    )
    enqueued = []

    def enqueue(*args, **kwargs):
        enqueued.append((args, kwargs))
        return SimpleNamespace(id=kwargs["job_id"])

    monkeypatch.setattr(queue_controller.default_queue, "enqueue", enqueue)

    job_id = queue_controller.enqueue_explicit_timeline_reconciliation("request-one")

    assert deleted == [True]
    assert job_id == "timeline-explicit_request-one"
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_blocked_request_never_imports_or_queues_work(requests_db, monkeypatch):
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=False, reason="immich_unconfigured")
        ),
    )
    imported = queued = 0

    async def import_candidates(*_args):
        nonlocal imported
        imported += 1

    def queue_request(*_args):
        nonlocal queued
        queued += 1

    monkeypatch.setattr(
        explicit_reconciliation, "import_immich_day_candidates", import_candidates
    )
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", queue_request)

    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user", active_timeline_pipeline="day"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert created is True
    assert request.state == "blocked"
    assert request.reason == "immich_unconfigured"
    assert imported == 0
    assert queued == 0


@pytest.mark.asyncio
async def test_ready_request_imports_before_queueing(requests_db, monkeypatch):
    events = []
    assets = [{"id": "photo"}]
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=True, reason="assets_on_day", assets=assets)
        ),
    )

    async def import_candidates(user_id, items):
        events.append(("import", user_id, items))
        return 1

    def queue_request(request_id):
        events.append(("queue", request_id))
        return "job-one"

    monkeypatch.setattr(
        explicit_reconciliation, "import_immich_day_candidates", import_candidates
    )
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", queue_request)

    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user", active_timeline_pipeline="day"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert created is True
    assert request.state == "queued"
    assert request.job_id == "job-one"
    assert [event[0] for event in events] == ["import", "queue"]


@pytest.mark.asyncio
async def test_queued_request_is_single_flight(requests_db, monkeypatch):
    existing = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        pipeline="day",
        state="queued",
        reason="assets_on_day",
    )
    await existing.insert()
    checks = 0

    async def check(*_args):
        nonlocal checks
        checks += 1

    monkeypatch.setattr(explicit_reconciliation, "check_immich_day_readiness", check)

    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user", active_timeline_pipeline="day"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert request.request_id == existing.request_id
    assert created is False
    assert checks == 0


@pytest.mark.asyncio
async def test_concurrent_requests_are_single_flight(requests_db, monkeypatch):
    lock = asyncio.Lock()
    lock_options = []

    @asynccontextmanager
    async def local_lock(*_args, **kwargs):
        lock_options.append(kwargs)
        async with lock:
            yield

    monkeypatch.setattr(explicit_reconciliation, "distributed_lock", local_lock)
    monkeypatch.setattr(
        explicit_reconciliation,
        "import_immich_day_candidates",
        lambda *_args: _async_value(0),
    )
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=True, reason="later_asset_watermark")
        ),
    )
    queued = []
    monkeypatch.setattr(
        explicit_reconciliation,
        "_queue_request",
        lambda request_id: queued.append(request_id) or "job-one",
    )

    results = await asyncio.gather(
        *(
            explicit_reconciliation.request_explicit_reconciliation(
                user=SimpleNamespace(id="user", active_timeline_pipeline="day"),
                local_date=date(2026, 8, 15),
                timezone_name="Asia/Kolkata",
            )
            for _ in range(2)
        )
    )

    assert [created for _request, created in results] == [True, False]
    assert results[0][0].request_id == results[1][0].request_id
    assert len(queued) == 1
    assert (
        lock_options
        == [
            {
                "timeout": explicit_reconciliation.EXPLICIT_REQUEST_LOCK_SECONDS,
                "blocking_timeout": explicit_reconciliation.EXPLICIT_REQUEST_LOCK_SECONDS,
            }
        ]
        * 2
    )


@pytest.mark.asyncio
async def test_concurrent_blocked_requests_reuse_one_readiness_snapshot(
    requests_db, monkeypatch
):
    lock = asyncio.Lock()
    checks = 0

    @asynccontextmanager
    async def local_lock(*_args, **_kwargs):
        async with lock:
            yield

    async def check(*_args):
        nonlocal checks
        checks += 1
        return readiness(ready=False, reason="immich_unconfigured")

    monkeypatch.setattr(explicit_reconciliation, "distributed_lock", local_lock)
    monkeypatch.setattr(explicit_reconciliation, "check_immich_day_readiness", check)

    results = await asyncio.gather(
        *(
            explicit_reconciliation.request_explicit_reconciliation(
                user=SimpleNamespace(id="user", active_timeline_pipeline="day"),
                local_date=date(2026, 8, 15),
                timezone_name="Asia/Kolkata",
            )
            for _ in range(2)
        )
    )

    assert checks == 1
    assert len({request.request_id for request, _created in results}) == 1
    assert [created for _request, created in results] == [True, False]


@pytest.mark.asyncio
async def test_non_owner_cannot_query_or_import_immich(requests_db, monkeypatch):
    monkeypatch.setattr(
        explicit_reconciliation,
        "resolve_immich_user_id",
        lambda: _async_value("configured-owner"),
    )
    checked = False

    async def check(*_args):
        nonlocal checked
        checked = True

    monkeypatch.setattr(explicit_reconciliation, "check_immich_day_readiness", check)

    with pytest.raises(PermissionError, match="configured Chronicle owner"):
        await explicit_reconciliation.request_explicit_reconciliation(
            user=SimpleNamespace(id="other-user", active_timeline_pipeline="day"),
            local_date=date(2026, 8, 15),
            timezone_name="Asia/Kolkata",
        )

    assert checked is False


@pytest.mark.asyncio
async def test_rolling_authorization_creates_an_exact_noncoalescing_day_range(
    requests_db, monkeypatch
):
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=True, reason="later_asset_watermark")
        ),
    )
    monkeypatch.setattr(
        explicit_reconciliation,
        "import_immich_day_candidates",
        lambda *_args: _async_value(0),
    )
    captured = {}

    async def mark(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        async def save():
            return None

        return SimpleNamespace(dirty_range_id="exact-day", save=save)

    monkeypatch.setattr(explicit_reconciliation, "mark_evidence_dirty", mark)
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", lambda _id: "job")

    request, _ = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user", active_timeline_pipeline="rolling"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert request.dirty_range_id == "exact-day"
    assert captured["kwargs"]["coalesce"] is False
    assert captured["args"][2] - captured["args"][1] == timedelta(days=1)


@pytest.mark.asyncio
async def test_real_explicit_worker_entrypoint_updates_durable_status(
    requests_db, monkeypatch
):
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        pipeline="day",
        state="queued",
        reason="assets_on_day",
        target_asset_count=1,
        immich_visual=ImmichVisualPreparationStatus(state="pending"),
    )
    await request.insert()
    run = SimpleNamespace(run_id="run-one")
    events = []

    async def prepare(*_args):
        events.append("visual")
        return ImmichVisualPreparation("complete", 1, 1, 1, 1, 0, 0)

    async def request_analysis(*_args, **_kwargs):
        events.append("analysis")
        return run

    async def process(*_args):
        events.append("process")
        return {"published": 2}

    captured = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)
    item = TimelineEvidenceItem(
        evidence_id="immich:photo",
        kind="immich",
        started_at=captured,
        role="user_action",
        metadata={"timeline_relevance": "high"},
    )
    manifest = TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        started_at=captured,
        ended_at=captured.replace(hour=9),
        evidence_revision="revision",
        evidence=[item],
        windows=[
            TimelineCoverageWindow(
                window_id="window",
                started_at=captured,
                ended_at=captured.replace(hour=9),
                evidence_ids=[item.evidence_id],
            )
        ],
    )
    monkeypatch.setattr(timeline_jobs, "prepare_immich_visual_evidence", prepare)
    monkeypatch.setattr(
        timeline_jobs,
        "request_timeline_analysis",
        request_analysis,
    )
    monkeypatch.setattr(
        timeline_jobs,
        "process_timeline_run",
        process,
    )
    monkeypatch.setattr(
        timeline_jobs,
        "assemble_day_evidence",
        lambda *_args, **_kwargs: _async_value((manifest, {})),
    )

    result = await timeline_jobs.explicit_reconciliation_job.__wrapped__(
        request.request_id
    )
    stored = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )

    assert result["state"] == "completed"
    assert stored.state == "completed"
    assert stored.run_id == "run-one"
    assert stored.immich_visual.helpful_count == 1
    assert stored.immich_evidence.window_count == 1
    assert events == ["visual", "analysis", "process"]


@pytest.mark.asyncio
async def test_real_worker_does_not_reconcile_when_all_visual_analysis_fails(
    requests_db, monkeypatch
):
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        pipeline="day",
        state="queued",
        reason="assets_on_day",
        target_asset_count=1,
        immich_visual=ImmichVisualPreparationStatus(state="pending"),
    )
    await request.insert()
    monkeypatch.setattr(
        timeline_jobs,
        "prepare_immich_visual_evidence",
        lambda *_args: _async_value(
            ImmichVisualPreparation("failed", 1, 0, 0, 0, 0, 1)
        ),
    )
    timeline_started = False

    async def request_analysis(*_args, **_kwargs):
        nonlocal timeline_started
        timeline_started = True

    monkeypatch.setattr(timeline_jobs, "request_timeline_analysis", request_analysis)

    with pytest.raises(RuntimeError, match="all selected Immich photos"):
        await timeline_jobs.explicit_reconciliation_job.__wrapped__(request.request_id)

    stored = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )
    assert stored.state == "failed"
    assert stored.immich_visual.state == "failed"
    assert timeline_started is False


@pytest.mark.asyncio
async def test_recoverable_rolling_failure_stays_queued_and_releases_range(
    requests_db, monkeypatch
):
    now = datetime.now(timezone.utc)
    dirty_range = DirtyEvidenceRange(
        user_id="user",
        started_at=now - timedelta(hours=1),
        ended_at=now,
        evidence_revision=1,
        not_before=now - timedelta(minutes=1),
        force_after=now - timedelta(minutes=1),
        dispatch_authorized_at=now,
    )
    await dirty_range.insert()
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        pipeline="rolling",
        state="queued",
        reason="later_asset_watermark",
        dirty_range_id=dirty_range.dirty_range_id,
    )
    await request.insert()
    dirty_range.reconciliation_request_id = request.request_id
    await dirty_range.save()

    async def fail_reconciliation(*_args, **_kwargs):
        raise RuntimeError("invalid TimelineAgentResult JSON")

    monkeypatch.setattr(timeline_jobs, "reconcile_range", fail_reconciliation)

    with pytest.raises(RuntimeError, match="invalid TimelineAgentResult JSON"):
        await timeline_jobs.explicit_reconciliation_job.__wrapped__(request.request_id)

    stored_request = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )
    stored_range = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range.dirty_range_id
    )
    assert stored_request.state == "queued"
    assert "invalid TimelineAgentResult JSON" in (stored_request.last_error or "")
    assert stored_range.state == "pending"
    assert stored_range.lease_owner is None
    assert "invalid TimelineAgentResult JSON" in (stored_range.last_error or "")


@pytest.mark.asyncio
async def test_exhausted_rolling_failure_is_terminal(requests_db, monkeypatch):
    now = datetime.now(timezone.utc)
    dirty_range = DirtyEvidenceRange(
        user_id="user",
        started_at=now - timedelta(hours=1),
        ended_at=now,
        evidence_revision=1,
        not_before=now - timedelta(minutes=1),
        force_after=now - timedelta(minutes=1),
        attempts=4,
        dispatch_authorized_at=now,
    )
    await dirty_range.insert()
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        pipeline="rolling",
        state="queued",
        reason="later_asset_watermark",
        dirty_range_id=dirty_range.dirty_range_id,
    )
    await request.insert()
    dirty_range.reconciliation_request_id = request.request_id
    await dirty_range.save()

    async def fail_reconciliation(*_args, **_kwargs):
        raise RuntimeError("still invalid")

    monkeypatch.setattr(timeline_jobs, "reconcile_range", fail_reconciliation)

    with pytest.raises(RuntimeError, match="still invalid"):
        await timeline_jobs.explicit_reconciliation_job.__wrapped__(request.request_id)

    stored_request = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )
    stored_range = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range.dirty_range_id
    )
    assert stored_request.state == "failed"
    assert stored_range.state == "failed"
    assert stored_range.attempts == 5


async def _async_value(value):
    return value
