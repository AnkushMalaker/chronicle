"""Uvicorn server construction with Chronicle's bounded shutdown policy."""

from threading import Event
from typing import Any

import uvicorn

# Long-lived SSE responses do not complete when Uvicorn starts a graceful stop.
# Give ordinary requests a short drain window, then cancel the remaining protocol
# tasks so FastAPI lifespan cleanup can run before the container's 15s ceiling.
CONNECTION_DRAIN_TIMEOUT_SECONDS = 2
_shutdown_requested = Event()


def shutdown_requested() -> bool:
    """Return whether the HTTP server has started its shutdown sequence."""
    return _shutdown_requested.is_set()


class ChronicleServer(uvicorn.Server):
    """Uvicorn server that asks Chronicle streams to drain before waiting."""

    async def shutdown(self, sockets=None) -> None:
        _shutdown_requested.set()
        await super().shutdown(sockets=sockets)


def create_server(app: Any, *, host: str, port: int) -> ChronicleServer:
    """Create Chronicle's HTTP server with one bounded connection-drain policy."""
    _shutdown_requested.clear()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        reload=False,
        access_log=False,
        log_level="info",
        timeout_graceful_shutdown=CONNECTION_DRAIN_TIMEOUT_SECONDS,
    )
    return ChronicleServer(config)
