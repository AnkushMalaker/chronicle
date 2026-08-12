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
from types import SimpleNamespace

import pytest

from advanced_omi_backend.client import ClientState
from advanced_omi_backend.client_manager import (
    ClientManager,
    generate_client_id,
    owns_client_id,
    synthetic_client_id,
)
from advanced_omi_backend.controllers import audio_controller


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


def test_upload_and_websocket_paths_mint_the_same_client_id():
    """One physical device must not get two identities by arriving two ways.

    The upload controller had its own ``generate_client_id`` that interpolated the raw
    device name, so a device whose name needed normalizing produced one id on upload
    and another over the WebSocket — two registry entries, two downlink channels, and
    conversations split across both. There is now one constructor; this asserts the
    normalization it applies, which is what the duplicate got wrong.
    """

    user = SimpleNamespace(id="507f1f77bcf86cd799a421c9")

    # Upper case, punctuation, and over-length names all normalize.
    assert generate_client_id(user, "MyPhone!") == "a421c9-myphone"
    assert generate_client_id(user, "phone") == "a421c9-phone"
    assert generate_client_id(user, "webui-recorder") == "a421c9-webui-reco"

    # The upload controller imports that same function rather than defining its own.
    assert audio_controller.generate_client_id is generate_client_id


def test_a_synthetic_client_id_is_not_normalized_like_a_device_name():
    """Server-controlled labels are not user input and must not be truncated.

    Routing ``annotation-import`` through the device constructor would silently
    shorten it to ``annotation`` and split existing conversations across two ids.
    """

    user = SimpleNamespace(id="507f1f77bcf86cd799a421c9")

    assert synthetic_client_id(user, "annotation-import") == "a421c9-annotation-import"
    assert generate_client_id(user, "annotation-import") == "a421c9-annotation"


def test_prefix_ownership_is_scoped_to_the_user():
    one = SimpleNamespace(id="507f1f77bcf86cd799a421c9")
    two = SimpleNamespace(id="507f1f77bcf86cd799b53d70")

    assert owns_client_id(one, "a421c9-phone") is True
    assert owns_client_id(one, "b53d70-phone") is False
    assert owns_client_id(two, "b53d70-phone") is True
    assert owns_client_id(one, "") is False
