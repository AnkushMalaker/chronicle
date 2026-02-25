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


@lru_cache(maxsize=1)
def is_langfuse_enabled() -> bool:
    """Check if Langfuse OTEL is configured."""
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
        and os.getenv("LANGFUSE_HOST")
    )


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
    """Initialize OTEL with configured exporters and OpenAI instrumentor.

    Supports multiple backends simultaneously:
    - Galileo: if GALILEO_API_KEY is set
    - Langfuse: if LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST are set

    Call once at app startup. No-op if no backends are configured.
    """
    galileo = is_galileo_enabled()
    langfuse = is_langfuse_enabled()

    if not galileo and not langfuse:
        logger.info("No OTEL backends configured (Galileo/Langfuse), skipping initialization")
        return

    try:
        from opentelemetry.sdk import trace as trace_sdk

        tracer_provider = trace_sdk.TracerProvider()
        backends = []

        # --- Galileo backend ---
        if galileo:
            try:
                from galileo import otel

                project = os.getenv("GALILEO_PROJECT", "chronicle")
                logstream = os.getenv("GALILEO_LOG_STREAM", "default")
                galileo_processor = otel.GalileoSpanProcessor(project=project, logstream=logstream)
                tracer_provider.add_span_processor(galileo_processor)
                backends.append("Galileo")
            except ImportError:
                logger.warning(
                    "Galileo packages not installed. " "Install with: uv pip install '.[galileo]'"
                )
            except Exception as e:
                logger.error(f"Failed to add Galileo span processor: {e}")

        # --- Langfuse backend ---
        if langfuse:
            try:
                from langfuse.opentelemetry import LangfuseSpanProcessor

                langfuse_processor = LangfuseSpanProcessor()
                tracer_provider.add_span_processor(langfuse_processor)
                backends.append("Langfuse")
            except ImportError:
                logger.warning(
                    "Langfuse OTEL packages not installed. " "Ensure langfuse>=3.13.0 is installed."
                )
            except Exception as e:
                logger.error(f"Failed to add Langfuse span processor: {e}")

        if not backends:
            logger.warning("No OTEL span processors were successfully added")
            return

        # Auto-instrument all OpenAI SDK calls (backend-agnostic)
        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor

            OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
        except ImportError:
            logger.warning(
                "OpenAI OTEL instrumentor not installed. "
                "Install with: uv pip install '.[galileo]'"
            )
            return

        global _otel_initialised
        _otel_initialised = True
        logger.info(
            f"OTEL initialized with {' + '.join(backends)} exporter(s) + OpenAI instrumentor"
        )
    except ImportError:
        logger.warning(
            "OTEL SDK packages not installed. " "Install opentelemetry-api and opentelemetry-sdk."
        )
    except Exception as e:
        logger.error(f"Failed to initialize OTEL: {e}")
