"""OpenTelemetry setup with Galileo span processor."""

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def is_galileo_enabled() -> bool:
    """Check if Galileo OTEL is configured."""
    return bool(os.getenv("GALILEO_API_KEY"))


_session_token = None


def set_galileo_session(session_id: str) -> None:
    """Set Galileo session ID so subsequent traces are grouped together."""
    global _session_token
    if not is_galileo_enabled():
        return
    try:
        from galileo.otel import _session_id_context

        _session_token = _session_id_context.set(session_id)
    except ImportError:
        pass


def clear_galileo_session() -> None:
    """Clear the Galileo session ID."""
    global _session_token
    if _session_token is None:
        return
    try:
        from galileo.otel import _session_id_context

        _session_id_context.reset(_session_token)
        _session_token = None
    except ImportError:
        pass


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

        logger.info("OTEL initialized with Galileo exporter + OpenAI instrumentor")
    except ImportError:
        logger.warning(
            "Galileo/OTEL packages not installed. "
            "Install with: uv pip install '.[galileo]'"
        )
    except Exception as e:
        logger.error(f"Failed to initialize OTEL: {e}")
