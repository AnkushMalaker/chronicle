"""Retry safety for Chronicle's internal Swiggy MCP client."""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.integrations.swiggy import (
    Bucket,
    MemoryTokenStore,
    Server,
    SwiggyClient,
    SwiggyError,
)
from advanced_omi_backend.integrations.swiggy import client as client_module
from advanced_omi_backend.integrations.swiggy.client import ToolResult


async def test_checkout_is_never_blindly_retried():
    client = SwiggyClient(MemoryTokenStore(), max_attempts=5)
    client._call_once = AsyncMock(
        side_effect=SwiggyError("upstream timeout", Bucket.UPSTREAM_TIMEOUT)
    )

    with pytest.raises(SwiggyError):
        await client.call(Server.INSTAMART, "checkout", addressId="address-1")

    assert client._call_once.await_count == 1


async def test_safe_read_retries_transient_failures(monkeypatch):
    client = SwiggyClient(MemoryTokenStore(), max_attempts=3)
    client._call_once = AsyncMock(
        side_effect=SwiggyError("upstream timeout", Bucket.UPSTREAM_TIMEOUT)
    )
    sleep = AsyncMock()
    monkeypatch.setattr(
        "advanced_omi_backend.integrations.swiggy.client.asyncio.sleep", sleep
    )

    with pytest.raises(SwiggyError):
        await client.call(Server.INSTAMART, "get_cart")

    assert client._call_once.await_count == 3
    assert sleep.await_count == 2


async def test_hung_mcp_call_becomes_a_bounded_upstream_timeout():
    client = SwiggyClient(
        MemoryTokenStore(),
        max_attempts=1,
        request_timeout_seconds=0.01,
    )

    async def never_returns(*_args, **_kwargs):
        await asyncio.Event().wait()

    client._call_once = AsyncMock(side_effect=never_returns)

    with pytest.raises(SwiggyError) as caught:
        await client.call(Server.INSTAMART, "get_cart")

    assert caught.value.bucket is Bucket.UPSTREAM_TIMEOUT
    assert client._call_once.await_count == 1


async def test_mcp_call_emits_privacy_safe_child_span(monkeypatch):
    client = SwiggyClient(MemoryTokenStore())
    client._call_once = AsyncMock(
        return_value=ToolResult("opaque provider response", {"items": []})
    )
    spans = []
    span_io = []
    span_attributes = []
    fake_span = object()

    @contextmanager
    def capture_span(name, **kwargs):
        spans.append((name, kwargs))
        yield fake_span

    monkeypatch.setattr(client_module, "chronicle_span", capture_span)
    monkeypatch.setattr(
        client_module,
        "set_span_io",
        lambda span, **kwargs: span_io.append((span, kwargs)),
    )
    monkeypatch.setattr(
        client_module,
        "set_span_attributes",
        lambda span, attributes: span_attributes.append((span, attributes)),
    )

    result = await client.call(
        Server.INSTAMART,
        "get_cart",
        addressId="private-address-id",
    )

    assert result.data == {"items": []}
    assert spans == [
        (
            "swiggy.mcp.call",
            {
                "tracer_name": "chronicle.integrations.swiggy",
                "attributes": {
                    "rpc.system": "mcp",
                    "rpc.method": "get_cart",
                    "server.address": "mcp.swiggy.com",
                    "chronicle.swiggy.server": "im",
                    "chronicle.swiggy.mutating": False,
                    "chronicle.swiggy.max_attempts": 5,
                },
            },
        )
    ]
    assert span_io[0] == (
        fake_span,
        {
            "input": {
                "server": "im",
                "tool": "get_cart",
                "argument_keys": ["addressId"],
            }
        },
    )
    assert span_io[-1][1]["output"] == {
        "data_type": "dict",
        "top_level_keys": ["items"],
        "item_count": None,
        "text_chars": len("opaque provider response"),
    }
    assert span_attributes[-1][1] == {
        "chronicle.swiggy.success": True,
        "chronicle.swiggy.attempts": 1,
    }
    captured = json.dumps(
        {"spans": spans, "io": span_io, "attributes": span_attributes},
        default=str,
    )
    assert "private-address-id" not in captured
    assert "opaque provider response" not in captured
