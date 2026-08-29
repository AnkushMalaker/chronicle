from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.routers.api_router import router as api_router
from advanced_omi_backend.services import client_diagnostics


@pytest.fixture
def route_app(tmp_path, monkeypatch):
    monkeypatch.setattr(client_diagnostics, "CLIENT_DIAGNOSTICS_DIR", tmp_path)
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[current_active_user] = lambda: SimpleNamespace(
        user_id="user-one"
    )
    return app


@pytest.mark.asyncio
async def test_phone_can_push_and_read_its_log(route_app, tmp_path):
    transport = httpx.ASGITransport(app=route_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/client-diagnostics",
            content=b"2026-08-29T10:00:00Z disconnect iosErrorCode=6\n",
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "X-Chronicle-Platform": "ios 26.0",
                "X-Chronicle-App-Version": "1.13.0",
                "X-Chronicle-Build-Version": "42",
                "X-Chronicle-Device-ID": "neosapient-1",
            },
        )

        assert response.status_code == 201
        receipt = response.json()
        assert receipt["platform"] == "ios 26.0"
        assert receipt["size_bytes"] == 47
        assert len(receipt["sha256"]) == 64

        upload_id = receipt["upload_id"]
        saved = tmp_path / "user-one" / f"{upload_id}.log"
        assert saved.read_text() == response.request.content.decode()

        listing = await client.get("/api/client-diagnostics")
        assert listing.status_code == 200
        assert listing.json()["items"][0]["upload_id"] == upload_id

        downloaded = await client.get(f"/api/client-diagnostics/{upload_id}")
        assert downloaded.status_code == 200
        assert downloaded.text == saved.read_text()


@pytest.mark.asyncio
async def test_phone_cannot_read_another_users_log(route_app, tmp_path):
    other = tmp_path / "user-two"
    other.mkdir()
    upload_id = "20260829T100000Z-123456789abc"
    (other / f"{upload_id}.log").write_text("private")

    transport = httpx.ASGITransport(app=route_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/client-diagnostics/{upload_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_push_rejects_empty_invalid_and_oversized_logs(route_app):
    transport = httpx.ASGITransport(app=route_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        empty = await client.post("/api/client-diagnostics", content=b"")
        invalid = await client.post("/api/client-diagnostics", content=b"\xff")
        oversized = await client.post(
            "/api/client-diagnostics",
            content=b"x" * (client_diagnostics.MAX_CLIENT_DIAGNOSTIC_BYTES + 1),
        )

    assert empty.status_code == 400
    assert invalid.status_code == 400
    assert oversized.status_code == 413
