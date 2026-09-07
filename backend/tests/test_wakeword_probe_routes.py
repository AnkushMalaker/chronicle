import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from backend.auth import current_active_user
from backend.routers.modules import wakeword_routes

pytestmark = pytest.mark.unit


async def test_authenticated_probe_proxy_resolves_owned_recorder_and_preserves_probe_interface(
    monkeypatch,
):
    upstream_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if request.method == "POST" and request.url.path == "/probes":
            body = json.loads(request.content)
            assert body == {
                "client_id": "owned-webui-recorder",
                "audio_session_id": "audio-current",
                "wakeword": "hermes",
                "timeout_seconds": 15.0,
            }
            return httpx.Response(
                201,
                json={
                    "probe_id": "probe-1",
                    "client_id": body["client_id"],
                    "wakeword": body["wakeword"],
                    "status": "listening",
                },
            )
        if request.method == "GET" and request.url.path == "/probes/probe-1":
            return httpx.Response(
                200,
                json={
                    "probe_id": "probe-1",
                    "client_id": "owned-webui-recorder",
                    "wakeword": "hermes",
                    "status": "detected",
                },
            )
        if request.method == "DELETE" and request.url.path == "/probes/probe-1":
            return httpx.Response(
                200,
                json={
                    "probe_id": "probe-1",
                    "client_id": "owned-webui-recorder",
                    "status": "cancelled",
                },
            )
        return httpx.Response(404, json={"detail": "missing"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        wakeword_routes,
        "_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://wake"),
    )
    monkeypatch.setattr(
        wakeword_routes,
        "_owns",
        lambda _user, client_id: client_id.startswith("owned-"),
    )
    route_app = FastAPI()
    route_app.include_router(wakeword_routes.router, prefix="/api")
    route_app.dependency_overrides[current_active_user] = lambda: SimpleNamespace()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=route_app), base_url="http://test"
    ) as client:
        started = await client.post(
            "/api/wakeword/probes",
            json={
                "client_id": "owned-webui-recorder",
                "audio_session_id": "audio-current",
                "wakeword": "hermes",
            },
        )
        assert started.status_code == 201
        assert started.json()["status"] == "listening"
        status = await client.get("/api/wakeword/probes/probe-1")
        assert status.status_code == 200
        assert status.json()["status"] == "detected"
        cancelled = await client.delete("/api/wakeword/probes/probe-1")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

    assert [request.url.path for request in upstream_requests] == [
        "/probes",
        "/probes/probe-1",
        "/probes/probe-1",
        "/probes/probe-1",
    ]


async def test_probe_status_cannot_cross_client_ownership(monkeypatch):
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "probe_id": "probe-other",
                "client_id": "other-webui-recorder",
                "status": "listening",
            },
        )
    )
    monkeypatch.setattr(
        wakeword_routes,
        "_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://wake"),
    )
    monkeypatch.setattr(
        wakeword_routes,
        "_owns",
        lambda _user, client_id: client_id.startswith("owned-"),
    )
    route_app = FastAPI()
    route_app.include_router(wakeword_routes.router, prefix="/api")
    route_app.dependency_overrides[current_active_user] = lambda: SimpleNamespace()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=route_app), base_url="http://test"
    ) as client:
        response = await client.get("/api/wakeword/probes/probe-other")
    assert response.status_code == 403
