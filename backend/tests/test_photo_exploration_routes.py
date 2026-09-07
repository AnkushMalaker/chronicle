import json
import os
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from beanie import init_beanie
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.timeline import (
    ImmichVisualPreparationStatus,
    TimelineReconciliationRequest,
)
from backend.routers.modules import timeline_routes


@pytest.mark.asyncio
async def test_real_photo_routes_scope_private_artifacts_to_request_owner(
    mongo_service, tmp_path, monkeypatch
):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_photo_routes"]
    await init_beanie(
        database=database, document_models=[TimelineReconciliationRequest]
    )
    monkeypatch.chdir(tmp_path)
    try:
        request = TimelineReconciliationRequest(
            user_id="owner",
            local_date=date(2026, 8, 4),
            timezone="Asia/Kolkata",
            state="completed",
            reason="assets_on_day",
            immich_visual=ImmichVisualPreparationStatus(
                state="complete", artifact_id="a" * 64
            ),
        )
        await request.insert()
        folder = (
            tmp_path / "data/timeline_photo_exploration/owner/2026-08-04" / ("a" * 64)
        )
        folder.mkdir(parents=True)
        (folder / "coverage.json").write_text(
            json.dumps({"inventory_count": 12, "inspected_count": 12})
        )
        (folder / "trace.json").write_text("[]")
        (folder / "round-01.png").write_bytes(b"grid-bytes")
        app = FastAPI()
        app.include_router(timeline_routes.router)
        app.dependency_overrides[timeline_routes.current_active_user] = (
            lambda: SimpleNamespace(id="owner")
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            prefix = f"/timeline/reconciliation/{request.request_id}/photos"
            response = await http.get(prefix)
            assert (
                response.status_code == 200
                and response.json()["coverage"]["inventory_count"] == 12
            )
            response = await http.get(prefix + "/1/grid")
            assert response.status_code == 200 and response.content == b"grid-bytes"
            assert response.headers["cache-control"].startswith("private")
            assert (await http.get(prefix + "/9/grid")).status_code == 404
            app.dependency_overrides[timeline_routes.current_active_user] = (
                lambda: SimpleNamespace(id="stranger")
            )
            assert (await http.get(prefix)).status_code == 404
            assert (await http.get(prefix + "/1/grid")).status_code == 404
    finally:
        await client.drop_database("test_photo_routes")
        client.close()
