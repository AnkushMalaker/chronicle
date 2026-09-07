"""Regression tests for plugins whose host is intentionally offline."""

import logging
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.plugins.base import PluginConnectivityError
from backend.services.plugin_service import discover_plugins


def _plugin_class(plugin_id: str):
    classes = discover_plugins()
    return classes[plugin_id]


@pytest.mark.asyncio
async def test_hermes_offline_initialization_is_retryable_not_recovered():
    """A dead Hermes host must keep the plugin degraded for background recovery."""
    plugin_class = _plugin_class("hermes")
    plugin = plugin_class(
        {
            "enabled": True,
            "api_url": "http://offline-rpi:8642",
        }
    )

    with patch.object(
        httpx.AsyncClient,
        "get",
        new=AsyncMock(side_effect=httpx.ConnectError("RPi is offline")),
    ):
        with pytest.raises(PluginConnectivityError, match="RPi is offline"):
            await plugin.initialize()

    await plugin.cleanup()


@pytest.mark.asyncio
async def test_homeassistant_transport_error_has_detail_without_error_log(caplog):
    """Expected offline probes should be descriptive without alarming every retry."""
    plugin_class = _plugin_class("homeassistant")
    plugin_module = sys.modules[plugin_class.__module__]
    client = plugin_module.HAMCPClient(
        base_url="http://offline-rpi:8123",
        token="test-token",
    )

    request = httpx.Request("POST", "http://offline-rpi:8123/api/template")
    failure = httpx.ConnectError("", request=request)
    with patch.object(client.client, "post", new=AsyncMock(side_effect=failure)):
        with caplog.at_level(
            logging.ERROR, logger=plugin_module.HAMCPClient.__module__
        ):
            with pytest.raises(plugin_module.MCPError, match="ConnectError"):
                await client._render_template("{{ 1 + 1 }}")

    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and "Request error rendering template" in record.getMessage()
    ]
    await client.close()
