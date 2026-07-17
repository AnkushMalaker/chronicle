"""Unit tests for the in-memory client lifecycle.

Covers the invariants the WebSocket evict-on-reconnect and idle-timeout/reaper
paths rely on:
  - a freshly created client is present, connected, and freshly stamped
  - touch() advances last_activity (drives the reaper + honest "connected")
  - remove_client_with_cleanup() is the single removal path and flips connected
  - create_client() rejects a duplicate id (the evict path must clean up first)

These are pure in-memory — no Redis, Mongo, or API keys required.
"""

import time

import pytest

from advanced_omi_backend.client import ClientState
from advanced_omi_backend.client_manager import ClientManager


def test_new_client_is_present_connected_and_fresh():
    mgr = ClientManager()
    before = time.time()
    state = mgr.create_client("u1-phone", "u1", "u1@example.com")

    assert mgr.has_client("u1-phone")
    assert state.connected is True
    assert state.last_activity >= before


def test_touch_advances_last_activity():
    state = ClientState("u1-phone", "u1", "u1@example.com")
    original = state.last_activity = time.time() - 100  # pretend it's been idle
    state.touch()
    assert state.last_activity > original


@pytest.mark.asyncio
async def test_remove_with_cleanup_disconnects_and_removes():
    mgr = ClientManager()
    state = mgr.create_client("u1-phone", "u1", "u1@example.com")

    removed = await mgr.remove_client_with_cleanup("u1-phone")

    assert removed is True
    assert not mgr.has_client("u1-phone")
    assert state.connected is False
    # Removing a now-absent client is a graceful no-op (idempotent reaper/evict).
    assert await mgr.remove_client_with_cleanup("u1-phone") is False


def test_create_duplicate_raises_so_evict_must_run_first():
    mgr = ClientManager()
    mgr.create_client("u1-phone", "u1", "u1@example.com")
    with pytest.raises(ValueError):
        mgr.create_client("u1-phone", "u1", "u1@example.com")
