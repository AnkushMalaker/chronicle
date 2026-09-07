"""Shared span helpers for tracing Chronicle subsystems into Langfuse.

Every function here is best-effort: telemetry must never fail the work it observes, so
span creation, attribute setting, and export errors are swallowed and logged at debug.

This is the generic home for what ``services/memory/telemetry.py`` grew first. Memory
still carries its own copy; new subsystems should use this module, and memory should be
migrated onto it rather than the two drifting apart.

OpenTelemetry stays an optional dependency — every import of it is lazy and guarded.
"""

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)

# Langfuse reads a span's input/output from these OTEL attributes.
LANGFUSE_INPUT = "langfuse.observation.input"
LANGFUSE_OUTPUT = "langfuse.observation.output"

_MAX_ATTRIBUTE_CHARS = 4000


def attribute_value(value: Any) -> Any:
    """Coerce a value into something an OTEL attribute can hold."""

    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return str(value)


def json_attribute(value: Any) -> str:
    try:
        rendered = json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 - telemetry only
        rendered = str(value)
    if len(rendered) > _MAX_ATTRIBUTE_CHARS:
        return rendered[:_MAX_ATTRIBUTE_CHARS] + "…"
    return rendered


def set_span_attributes(span: Any, attributes: Mapping[str, Any]) -> None:
    """Set attributes without letting telemetry raise into the caller."""

    if span is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            span.set_attribute(str(key), attribute_value(value))
        except Exception:  # noqa: BLE001 - telemetry only
            logger.debug("failed to set span attribute %s", key, exc_info=True)


def set_span_io(span: Any, *, input: Any = None, output: Any = None) -> None:
    """Attach Langfuse input/output payloads to a span."""

    attributes: dict[str, Any] = {}
    if input is not None:
        attributes[LANGFUSE_INPUT] = json_attribute(input)
    if output is not None:
        attributes[LANGFUSE_OUTPUT] = json_attribute(output)
    set_span_attributes(span, attributes)


def set_span_usage(span: Any, usage: Mapping[str, Any]) -> None:
    """Record model token usage in the fields Langfuse aggregates cost from."""

    if not usage:
        return
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total = None
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        total = input_tokens + output_tokens
    set_span_attributes(
        span,
        {
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "gen_ai.usage.total_tokens": total,
            # Not part of the OTEL gen_ai convention, but the number that explains a
            # surprising bill: cached input is billed differently from fresh input.
            "chronicle.usage.cached_input_tokens": usage.get("cached_input_tokens"),
            "chronicle.usage.reasoning_output_tokens": usage.get(
                "reasoning_output_tokens"
            ),
        },
    )


@contextmanager
def chronicle_span(
    name: str,
    *,
    tracer_name: str = "chronicle",
    attributes: Optional[Mapping[str, Any]] = None,
) -> Iterator[Any]:
    """Start one span, make it current, and end it however the block exits.

    Exceptions from the wrapped work still propagate; only the exception class is
    recorded, so provider responses cannot leak endpoints or credentials into traces.
    """

    span = None
    token = None
    try:
        # Resolved per call so a later init_otel() swaps in the real provider.
        from backend.observability.otel_setup import get_tracer

        tracer = get_tracer(tracer_name)
        if tracer is not None:
            span = tracer.start_span(
                name,
                attributes={
                    str(key): attribute_value(value)
                    for key, value in (attributes or {}).items()
                    if value is not None
                },
            )
            try:
                # OpenTelemetry is an optional dependency, so every use is lazy.
                from opentelemetry.context import attach
                from opentelemetry.trace import set_span_in_context

                token = attach(set_span_in_context(span))
            except Exception:  # noqa: BLE001 - span is still useful unattached
                token = None
    except Exception:  # noqa: BLE001 - telemetry must never fail the work
        logger.debug("failed to start span %s", name, exc_info=True)
        span = None

    try:
        yield span
    except BaseException as exc:
        set_span_attributes(span, {"error.type": type(exc).__name__, "success": False})
        if span is not None:
            try:
                # Optional dependency; imported lazily like the rest of OTEL here.
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR))
            except Exception:  # noqa: BLE001 - telemetry only
                pass
        raise
    finally:
        if token is not None:
            try:
                # Optional dependency; imported lazily like the rest of OTEL here.
                from opentelemetry.context import detach

                detach(token)
            except Exception:  # noqa: BLE001 - telemetry only
                logger.debug("failed to detach trace context", exc_info=True)
        if span is not None:
            try:
                span.end()
            except Exception:  # noqa: BLE001 - telemetry only
                logger.debug("failed to end span %s", name, exc_info=True)
