"""Privacy-safe OpenTelemetry helpers for Chronicle memory agents.

The memory system runs through three very different executors: Python/OpenAI,
Codex CLI, and Pi's Node subprocess.  This module gives all three the same trace
shape without making observability a runtime dependency or a failure mode.

Manual spans contain derived metadata by default.  Personal text is included only
when ``LANGFUSE_MEMORY_CAPTURE_CONTENT=true`` and is always bounded.  Credentials,
provider URLs, and arbitrary exception strings must never be passed to these helpers.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional

logger = logging.getLogger("memory_service.telemetry")

_CAPTURE_CONTENT_ENV = "LANGFUSE_MEMORY_CAPTURE_CONTENT"
_CONTENT_LIMIT_ENV = "LANGFUSE_MEMORY_CONTENT_MAX_CHARS"
_DEFAULT_CONTENT_LIMIT = 16_000
_MAX_CONTENT_LIMIT = 65_536
_MAX_ATTRIBUTE_CHARS = 70_000
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_attempt_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "chronicle_memory_attempt", default="primary"
)


def memory_content_capture_enabled() -> bool:
    """Return whether personal memory text may be sent to the local trace store."""

    return os.getenv(_CAPTURE_CONTENT_ENV, "").strip().lower() in _TRUE_VALUES


def _content_limit() -> int:
    raw = os.getenv(_CONTENT_LIMIT_ENV, "").strip()
    if not raw:
        return _DEFAULT_CONTENT_LIMIT
    try:
        parsed = int(raw)
    except ValueError:
        return _DEFAULT_CONTENT_LIMIT
    return max(1, min(parsed, _MAX_CONTENT_LIMIT))


def text_payload(value: Optional[str]) -> Dict[str, Any]:
    """Represent personal text with stable metadata and optional bounded content."""

    text = value or ""
    payload: Dict[str, Any] = {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    if memory_content_capture_enabled():
        limit = _content_limit()
        payload["content"] = text[:limit]
        payload["content_truncated"] = len(text) > limit
    return payload


def _attribute_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value[:_MAX_ATTRIBUTE_CHARS] if isinstance(value, str) else value
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, (str, bool, int, float)) for item in value
    ):
        return [
            item[:_MAX_ATTRIBUTE_CHARS] if isinstance(item, str) else item
            for item in value
        ]
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return encoded[:_MAX_ATTRIBUTE_CHARS]


def set_safe_span_attributes(span: Any, attributes: Mapping[str, Any]) -> None:
    """Set OTEL-compatible values without allowing telemetry to fail the caller."""

    if span is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            span.set_attribute(str(key), _attribute_value(value))
        except Exception:  # noqa: BLE001 - telemetry must never fail memory work
            logger.debug("failed to set memory span attribute %s", key, exc_info=True)


def _json_attribute(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) <= _MAX_ATTRIBUTE_CHARS:
        return encoded
    # Never slice JSON mid-token: Langfuse should always receive a parseable value,
    # even when an operator raises the opt-in content limit and combines several
    # personal-content fields in one observation.
    return json.dumps(
        {
            "chars": len(encoded),
            "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "truncated": True,
        },
        sort_keys=True,
    )


def set_observation_io(
    span: Any,
    *,
    input: Any = None,
    output: Any = None,
) -> None:
    """Populate Langfuse observation tabs with already-sanitized payloads."""

    attributes: Dict[str, str] = {}
    if input is not None:
        attributes["langfuse.observation.input"] = _json_attribute(input)
    if output is not None:
        attributes["langfuse.observation.output"] = _json_attribute(output)
    set_safe_span_attributes(span, attributes)


def current_otel_context() -> Any:
    """Capture the active OTEL context for work that will cross a thread boundary."""

    try:
        from opentelemetry.context import get_current

        return get_current()
    except Exception:  # noqa: BLE001 - optional dependency / telemetry only
        return None


@contextmanager
def memory_attempt(role: str) -> Iterator[None]:
    """Label nested executor spans as a primary or recovery attempt."""

    token = _attempt_var.set(str(role or "primary"))
    try:
        yield
    finally:
        _attempt_var.reset(token)


def current_memory_attempt() -> str:
    return _attempt_var.get()


@contextmanager
def memory_span(
    name: str,
    *,
    attributes: Optional[Mapping[str, Any]] = None,
    parent_context: Any = None,
) -> Iterator[Any]:
    """Start one robust memory span and make it current for nested operations.

    Span creation, context attachment, attribute updates, detach, and end are all
    best-effort.  Exceptions from the wrapped memory operation still propagate, but
    only their class is recorded so provider responses cannot leak endpoints or keys.
    """

    span = None
    token = None
    try:
        from advanced_omi_backend.observability.otel_setup import get_tracer

        tracer = get_tracer("chronicle.memory")
        if tracer is not None:
            span = tracer.start_span(
                name,
                context=parent_context,
                attributes={
                    str(key): _attribute_value(value)
                    for key, value in (attributes or {}).items()
                    if value is not None
                },
            )
            try:
                from opentelemetry.context import attach
                from opentelemetry.trace import set_span_in_context

                token = attach(set_span_in_context(span, parent_context))
            except Exception:  # noqa: BLE001 - span can still be useful without attach
                token = None
    except Exception:  # noqa: BLE001 - telemetry must never fail memory work
        logger.debug("failed to start memory span %s", name, exc_info=True)
        span = None

    try:
        yield span
    except BaseException as exc:
        set_safe_span_attributes(
            span,
            {
                "error.type": type(exc).__name__,
                "chronicle.memory.success": False,
            },
        )
        if span is not None:
            try:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR))
            except Exception:  # noqa: BLE001 - telemetry only
                pass
        raise
    finally:
        if token is not None:
            try:
                from opentelemetry.context import detach

                detach(token)
            except Exception:  # noqa: BLE001 - telemetry only
                logger.debug("failed to detach memory trace context", exc_info=True)
        if span is not None:
            try:
                span.end()
            except Exception:  # noqa: BLE001 - telemetry only
                logger.debug("failed to end memory span %s", name, exc_info=True)


def record_llm_usage_span(
    name: str,
    *,
    provider: str,
    model: str,
    usage: Mapping[str, Any],
    start_time_ns: int,
    end_time_ns: int,
    attributes: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emit aggregate subprocess tokens on a child LLM span for Langfuse rollups."""

    clean_usage = {
        str(key): int(value)
        for key, value in usage.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    }
    if not clean_usage:
        return
    try:
        from advanced_omi_backend.observability.otel_setup import get_tracer

        tracer = get_tracer("chronicle.memory")
        if tracer is None:
            return
        span_attributes: Dict[str, Any] = {
            "openinference.span.kind": "LLM",
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": model,
            "gen_ai.response.model": model,
            **{f"gen_ai.usage.{key}": value for key, value in clean_usage.items()},
        }
        span_attributes.update(attributes or {})
        span = tracer.start_span(
            name,
            attributes={
                str(key): _attribute_value(value)
                for key, value in span_attributes.items()
                if value is not None
            },
            start_time=start_time_ns,
        )
        span.end(end_time=end_time_ns)
    except Exception:  # noqa: BLE001 - telemetry must never fail memory work
        logger.debug("failed to record memory LLM usage span", exc_info=True)
