"""How the reconcile-status endpoint reads its ``dry_run`` flag.

This flag guards a write over every conversation, so it has exactly one direction it
must never fail in: a caller who asked for a preview must get a preview.

It used to be declared ``Body(False, embed=True)`` while every other ``dry_run`` in the
API is a query parameter. FastAPI therefore ignored ``?dry_run=true`` and fell back to
the default, so the request that most obviously reads as "show me what you would do"
silently wrote instead. The bug was in the *binding*, which is why these go through a
real request rather than calling the handler directly — the latter would have passed
against the broken version.
"""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from advanced_omi_backend.auth import current_superuser
from advanced_omi_backend.routers.modules import system_routes

pytestmark = pytest.mark.unit


@pytest.fixture
def client(monkeypatch):
    """The real route, with auth stubbed and the reconciler replaced by a spy."""
    seen: dict = {}

    async def _spy(*, dry_run: bool = False, **kwargs):
        seen["dry_run"] = dry_run
        return {"scanned": 0, "changed": 0, "dry_run": dry_run, "details": []}

    monkeypatch.setattr(system_routes, "reconcile_conversation_statuses", _spy)

    app = FastAPI()
    app.include_router(system_routes.router, prefix="/api")
    app.dependency_overrides[current_superuser] = lambda: None

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test"), seen


URL = "/api/admin/conversations/reconcile-status"


async def test_a_preview_asked_for_in_the_query_is_honoured(client):
    """The regression: this exact request used to write."""
    http, seen = client
    async with http:
        response = await http.post(f"{URL}?dry_run=true")

    assert response.status_code == 200
    assert seen["dry_run"] is True
    assert response.json()["dry_run"] is True


async def test_a_preview_asked_for_in_the_body_is_honoured(client):
    http, seen = client
    async with http:
        response = await http.post(URL, json={"dry_run": True})

    assert response.status_code == 200
    assert seen["dry_run"] is True


async def test_a_preview_requested_either_way_wins(client):
    """Disagreement resolves toward not writing, never toward writing."""
    http, seen = client
    async with http:
        await http.post(f"{URL}?dry_run=true", json={"dry_run": False})

    assert seen["dry_run"] is True


async def test_asking_for_neither_still_applies_the_fix(client):
    """The endpoint's purpose is to repair drift, so the bare call must still write."""
    http, seen = client
    async with http:
        response = await http.post(URL)

    assert response.status_code == 200
    assert seen["dry_run"] is False
