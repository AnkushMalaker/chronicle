"""Device-input completion crosses the durable Timeline context lifecycle seam."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.device_input import DeviceInputItem, DeviceInputJob
from backend.models.timeline import (
    DirtyEvidenceRange,
    EvidenceLocator,
    TimelineReconciliationRequest,
)
from backend.routers.modules import device_input_routes
from backend.services.timeline import dirty_ranges
from backend.services.timeline.contracts import StageContextRequest


class _RedisCounterFake:
    def __init__(self):
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str):
        return self.values.get(key)

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def context_documents(mongo_service, monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_timeline_context_acquisition_db"]
    await init_beanie(
        database=database,
        document_models=[
            DirtyEvidenceRange,
            DeviceInputJob,
            DeviceInputItem,
            TimelineReconciliationRequest,
        ],
    )
    counter = _RedisCounterFake()
    monkeypatch.setattr(dirty_ranges, "create_async_redis", lambda **_: counter)
    monkeypatch.setattr(
        device_input_routes,
        "select_context_items",
        lambda items, **_kwargs: (
            items,
            {"over_budget": 0, "duplicate": 0, "kept": len(items)},
        ),
    )
    yield counter
    await client.drop_database(database.name)
    client.close()


@pytest.mark.asyncio
async def test_device_completion_persists_then_authorizes_refenced_successor(
    context_documents,
):
    start = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    parent = await dirty_ranges.authorize_explicit_range(
        user_id="user",
        started_at=start,
        ended_at=start + timedelta(minutes=10),
        reconciliation_request_id="explicit-one",
        reason="manual",
    )
    leased = await dirty_ranges.lease_authorized_range_by_id(
        parent.dirty_range_id, "worker"
    )
    request = dirty_ranges.bind_context_request(
        leased,
        StageContextRequest(
            context_request_id="context-one",
            hypothesis_id="hypothesis-one",
            stage="separation",
            locator=EvidenceLocator(
                capture_source_id="screenpipe-one",
                modality="screen",
                track_id="display-one",
            ),
            started_at=start - timedelta(minutes=2),
            ended_at=start + timedelta(minutes=12),
            base_manifest_hash="manifest-one",
            leased_evidence_revision=leased.leased_evidence_revision,
            target_resolution="one_frame_per_10_seconds",
            max_items=12,
            reason="transition needs a denser sample",
        ),
    )
    await dirty_ranges.park_for_context(leased, request)
    job = await DeviceInputJob.find_one(
        DeviceInputJob.context_request_id == request.context_request_id
    )
    source = SimpleNamespace(user_id="user", source_id="screenpipe-one")
    body = device_input_routes.JobCompletion(
        items=[
            device_input_routes.ActivityItem(
                source_item_id="frame-one",
                locator=EvidenceLocator(
                    capture_source_id="screenpipe-one",
                    modality="screen",
                    track_id="display-one",
                ),
                captured_at=start - timedelta(minutes=1),
                ended_at=start,
                metadata={"display_id": "display-one", "text": "Editor"},
            )
        ]
    )

    result = await device_input_routes.complete_job(str(job.id), body, source)
    assert result["stored"] == 1
    stored_item = await DeviceInputItem.find_one(
        DeviceInputItem.source_item_id == "frame-one"
    )
    stored_job = await DeviceInputJob.get(job.id)
    stored_parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == parent.dirty_range_id
    )
    successor = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == stored_parent.superseded_by_dirty_range_id
    )
    assert stored_item is not None
    assert stored_job.status == "complete"
    assert stored_job.payload["result_evidence_ids"] == [
        f"observation:{stored_item.id}"
    ]
    assert stored_parent.state == "superseded"
    assert successor.state == "authorized_pending"
    assert successor.context_requests[0].result_evidence_ids == [
        f"observation:{stored_item.id}"
    ]

    # A collector replay observes the durable result; it neither stores another item
    # nor advances the evidence fence a second time.
    revision = successor.evidence_revision
    replay = await device_input_routes.complete_job(str(job.id), body, source)
    reloaded = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == successor.dirty_range_id
    )
    assert replay["stored"] == 1
    assert await DeviceInputItem.find_all().count() == 1
    assert reloaded.evidence_revision == revision


@pytest.mark.asyncio
async def test_device_completion_rejects_duplicate_with_different_locator(
    context_documents,
):
    captured_at = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    existing = DeviceInputItem(
        user_id="user",
        source_id="screenpipe-one",
        kind="screen_context",
        source_item_id="frame-one",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-one",
            modality="screen",
            track_id="display-two",
        ),
        captured_at=captured_at,
    )
    await existing.insert()
    job = DeviceInputJob(
        user_id="user",
        source_id="screenpipe-one",
        kind="screen_context",
        purpose="resolve ambiguous transition",
        payload={
            "locator": EvidenceLocator(
                capture_source_id="screenpipe-one",
                modality="screen",
                track_id="display-one",
            ).model_dump(mode="json")
        },
        status="claimed",
    )
    await job.insert()
    source = SimpleNamespace(user_id="user", source_id="screenpipe-one")
    body = device_input_routes.JobCompletion(
        items=[
            device_input_routes.ActivityItem(
                source_item_id="frame-one",
                locator=EvidenceLocator(
                    capture_source_id="screenpipe-one",
                    modality="screen",
                    track_id="display-one",
                ),
                captured_at=captured_at,
            )
        ]
    )

    with pytest.raises(device_input_routes.HTTPException) as exc_info:
        await device_input_routes.complete_job(str(job.id), body, source)

    assert exc_info.value.status_code == 409
    assert "identity conflicts" in exc_info.value.detail
    stored_job = await DeviceInputJob.get(job.id)
    stored_item = await DeviceInputItem.get(existing.id)
    assert stored_job.status == "claimed"
    assert stored_item.locator.track_id == "display-two"
    assert await DeviceInputItem.find_all().count() == 1
