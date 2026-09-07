"""Small, typed wrapper around Swiggy's streamable-HTTP MCP servers."""

from __future__ import annotations

import asyncio
import json
import random
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, AsyncIterator, Callable

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from backend.observability.tracing import (
    chronicle_span,
    set_span_attributes,
    set_span_io,
)

from .auth import build_oauth_provider
from .errors import Bucket, SwiggyAuthError, SwiggyError, classify, error_from_envelope
from .tokens import TokenStore

BASE_URL = "https://mcp.swiggy.com"
_BASE_DELAY = 0.5
_MAX_DELAY = 8.0
_MAX_ATTEMPTS = 5
_REQUEST_TIMEOUT_SECONDS = 15.0

# These calls can create/finalize a paid order.  A transport timeout does not
# prove the server failed to act, so blindly replaying them could place twice.
_NEVER_RETRY_TOOLS = frozenset({"checkout", "confirm_order"})
_MUTATING_TOOLS = frozenset({"update_cart", "clear_cart", "checkout", "confirm_order"})


class Server(str, Enum):
    FOOD = "food"
    INSTAMART = "im"
    DINEOUT = "dineout"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.value}"


class ToolResult:
    def __init__(self, text: str, data: dict | list | None):
        self.text = text
        self.data = data

    def __repr__(self) -> str:
        kind = "json" if self.data is not None else "text"
        return f"<ToolResult {kind} {len(self.text)} chars>"


def _parse(text: str) -> dict | list | None:
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _unwrap(text: str) -> ToolResult:
    parsed = _parse(text)
    if isinstance(parsed, dict) and "success" in parsed:
        if not parsed.get("success"):
            raise error_from_envelope(parsed)
        payload = parsed.get("data")
        return ToolResult(text, payload if isinstance(payload, (dict, list)) else None)
    return ToolResult(text, parsed)


class SwiggyClient:
    def __init__(
        self,
        store: TokenStore,
        *,
        session_factory: Callable[[Server], Any] | None = None,
        max_attempts: int = _MAX_ATTEMPTS,
        request_timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS,
    ):
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._store = store
        self._session_factory = session_factory or self._connect
        self._max_attempts = max_attempts
        self._request_timeout_seconds = float(request_timeout_seconds)

    @asynccontextmanager
    async def _connect(self, server: Server) -> AsyncIterator[ClientSession]:
        oauth = build_oauth_provider(server.url, self._store)
        http_client = create_mcp_http_client(auth=oauth)
        async with streamable_http_client(server.url, http_client=http_client) as (
            read,
            write,
            *_,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def list_tools(self, server: Server) -> list:
        async with self._session_factory(server) as session:
            return (await session.list_tools()).tools

    async def call(self, server: Server, tool: str, **arguments: Any) -> ToolResult:
        """Call a tool, retrying transient failures only when replay is safe."""
        attempts = 1 if tool in _NEVER_RETRY_TOOLS else self._max_attempts
        with chronicle_span(
            "swiggy.mcp.call",
            tracer_name="chronicle.integrations.swiggy",
            attributes={
                "rpc.system": "mcp",
                "rpc.method": tool,
                "server.address": "mcp.swiggy.com",
                "chronicle.swiggy.server": server.value,
                "chronicle.swiggy.mutating": tool in _MUTATING_TOOLS,
                "chronicle.swiggy.max_attempts": attempts,
            },
        ) as span:
            set_span_io(
                span,
                input={
                    "server": server.value,
                    "tool": tool,
                    "argument_keys": sorted(arguments),
                },
            )
            last: SwiggyError | None = None
            completed_attempts = 0
            for attempt in range(attempts):
                completed_attempts = attempt + 1
                try:
                    try:
                        result = await asyncio.wait_for(
                            self._call_once(server, tool, arguments),
                            timeout=self._request_timeout_seconds,
                        )
                    except asyncio.TimeoutError as exc:
                        raise SwiggyError(
                            f"{tool} timed out after "
                            f"{self._request_timeout_seconds:g} seconds",
                            Bucket.UPSTREAM_TIMEOUT,
                        ) from exc
                except SwiggyAuthError as exc:
                    _record_failure(span, exc, completed_attempts)
                    raise
                except SwiggyError as exc:
                    if not exc.retryable:
                        _record_failure(span, exc, completed_attempts)
                        raise
                    last = exc
                    if completed_attempts < attempts:
                        await asyncio.sleep(_backoff(attempt))
                else:
                    set_span_attributes(
                        span,
                        {
                            "chronicle.swiggy.success": True,
                            "chronicle.swiggy.attempts": completed_attempts,
                        },
                    )
                    set_span_io(span, output=_result_trace_summary(result))
                    return result
            assert last is not None
            _record_failure(span, last, completed_attempts)
            raise last

    async def _call_once(
        self, server: Server, tool: str, arguments: dict
    ) -> ToolResult:
        async with self._session_factory(server) as session:
            result = await session.call_tool(tool, arguments)

        text = "\n".join(
            block.text for block in result.content if hasattr(block, "text")
        )
        if getattr(result, "isError", False):
            raise SwiggyError(text or f"{tool} failed", classify(text))

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            if "success" in structured and not structured.get("success"):
                raise error_from_envelope(structured)
            payload = structured.get("data", structured)
            return ToolResult(
                text, payload if isinstance(payload, (dict, list)) else None
            )
        return _unwrap(text)


def _backoff(attempt: int) -> float:
    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
    return delay * (0.5 + random.random() / 2)


def _record_failure(span: Any, error: SwiggyError, attempts: int) -> None:
    set_span_attributes(
        span,
        {
            "chronicle.swiggy.success": False,
            "chronicle.swiggy.attempts": attempts,
            "chronicle.swiggy.error_bucket": error.bucket.value,
        },
    )
    set_span_io(
        span,
        output={"success": False, "error_bucket": error.bucket.value},
    )


def _result_trace_summary(result: ToolResult) -> dict[str, Any]:
    data = result.data
    return {
        "data_type": type(data).__name__,
        "top_level_keys": sorted(data) if isinstance(data, dict) else [],
        "item_count": len(data) if isinstance(data, list) else None,
        "text_chars": len(result.text),
    }


__all__ = [
    "Bucket",
    "Server",
    "SwiggyAuthError",
    "SwiggyClient",
    "SwiggyError",
    "ToolResult",
]
