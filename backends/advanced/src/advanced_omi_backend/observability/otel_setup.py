"""OpenTelemetry setup and session management.

Uses OpenInference semantic conventions (session.id) so that any
compatible observability backend (Galileo, Arize Phoenix, Langfuse, etc.)
can group traces by session.
"""

import contextvars
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

_otel_initialised = False

# Per-task/thread token so concurrent conversations don't clobber each other.
_session_token_var: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "_otel_session_token", default=None
)


@lru_cache(maxsize=1)
def is_galileo_enabled() -> bool:
    """Check if Galileo OTEL is configured."""
    return bool(os.getenv("GALILEO_API_KEY"))


def is_otel_enabled() -> bool:
    """Check if any OTel exporter has been initialised."""
    return _otel_initialised


def set_otel_session(session_id: str) -> None:
    """Attach *session_id* to the OTel context (OpenInference ``session.id``).

    All subsequent spans on this thread/context will carry the session ID,
    regardless of which observability backend is consuming them.
    Safe to call concurrently from different asyncio tasks or threads.
    """
    if not is_otel_enabled():
        return
    try:
        from openinference.semconv.trace import SpanAttributes
        from opentelemetry.context import attach, get_current, set_value

        clear_otel_session()
        ctx = set_value(SpanAttributes.SESSION_ID, session_id, get_current())
        _session_token_var.set(attach(ctx))
    except ImportError:
        pass


def clear_otel_session() -> None:
    """Detach the current session from the OTel context."""
    token = _session_token_var.get()
    if token is None:
        return
    try:
        from opentelemetry.context import detach

        detach(token)
        _session_token_var.set(None)
    except Exception:
        _session_token_var.set(None)


def init_otel() -> None:
    """Initialize OTEL with Galileo exporter and OpenAI instrumentor.

    Call once at app startup. Safe to call if Galileo is not configured (no-op).
    Filters out embedding spans — only LLM (chat completion) calls are exported.
    """
    if not is_galileo_enabled():
        logger.info("Galileo not configured, skipping OTEL initialization")
        return

    try:
        from galileo import otel
        from openinference.instrumentation.openai import OpenAIInstrumentor
        from opentelemetry import context
        from opentelemetry.sdk import trace as trace_sdk
        from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

        project = os.getenv("GALILEO_PROJECT", "chronicle")
        logstream = os.getenv("GALILEO_LOG_STREAM", "default")

        class _LLMOnlyProcessor(SpanProcessor):
            """Wraps GalileoSpanProcessor, dropping EMBEDDING spans."""

            def __init__(self, inner: SpanProcessor):
                self._inner = inner

            def on_start(
                self, span: Span, parent_context: context.Context | None = None
            ) -> None:
                self._inner.on_start(span, parent_context)

            def on_end(self, span: ReadableSpan) -> None:
                kind = span.attributes.get("openinference.span.kind", "")
                if kind == "EMBEDDING":
                    return  # drop
                self._inner.on_end(span)

            def shutdown(self) -> None:
                self._inner.shutdown()

            def force_flush(self, timeout_millis: int = 30000) -> bool:
                return self._inner.force_flush(timeout_millis)

        tracer_provider = trace_sdk.TracerProvider()
        galileo_processor = otel.GalileoSpanProcessor(
            project=project, logstream=logstream
        )
        tracer_provider.add_span_processor(_LLMOnlyProcessor(galileo_processor))

        # Auto-instrument all OpenAI SDK calls
        OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)

        global _otel_initialised
        _otel_initialised = True
        logger.info("OTEL initialized with Galileo exporter + OpenAI instrumentor")
    except ImportError:
        logger.warning(
            "Galileo/OTEL packages not installed. "
            "Install with: uv pip install '.[galileo]'"
        )
    except Exception as e:
        logger.error(f"Failed to initialize OTEL: {e}")
