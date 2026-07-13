"""OpenTelemetry setup, session management, and tracing helpers.

Uses OpenInference semantic conventions (session.id) so that any
compatible observability backend (Galileo, Arize Phoenix, Langfuse, etc.)
can group traces by session.

Provides two helpers for instrumenting pipeline stages:
- ``@traced_job(name, **attrs)`` — decorator wrapping an async function in a span
- ``set_span_attrs(**attrs)`` — enrich the current active span with namespaced attributes
"""

import contextvars
import functools
import json
import logging
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Sequence

logger = logging.getLogger(__name__)

# NOTE: All opentelemetry / openinference imports in this module are done lazily
# inside the functions below (guarded by try/except ImportError) so the backend
# runs fine when the optional OTel dependencies are not installed.

# Surface silent Galileo SDK errors (it catches exceptions internally)
logging.getLogger("galileo.utils.catch_log").setLevel(logging.WARNING)

_otel_initialised = False
_tracer_provider = None

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


@lru_cache(maxsize=1)
def _debug_otel_dir() -> Path | None:
    """Return the debug OTEL directory if configured, else None."""
    raw = os.getenv("DEBUG_OTEL_DIR", "").strip()
    if not raw:
        return None
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


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


class _FileSpanExporter:
    """Writes completed spans as JSONL to DEBUG_OTEL_DIR.

    One file per PID so concurrent workers don't clobber each other.
    Implements the SpanExporter protocol without importing the SDK at module level.
    """

    def __init__(self, directory: Path) -> None:
        self._path = directory / f"spans_{os.getpid()}.jsonl"
        self._lock = threading.Lock()
        logger.info(f"Debug OTEL file exporter writing to {self._path}")

    @staticmethod
    def _span_to_dict(span) -> Dict[str, Any]:
        ctx = span.get_span_context()
        return {
            "name": span.name,
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
            "parent_span_id": (
                format(span.parent.span_id, "016x") if span.parent else None
            ),
            "start_time": span.start_time,
            "end_time": span.end_time,
            "status": str(span.status.status_code) if span.status else None,
            "attributes": dict(span.attributes) if span.attributes else {},
            "resource": (dict(span.resource.attributes) if span.resource else {}),
            "events": [
                {
                    "name": e.name,
                    "attributes": dict(e.attributes) if e.attributes else {},
                }
                for e in (span.events or [])
            ],
        }

    def export(self, spans: Sequence) -> int:
        with self._lock:
            with open(self._path, "a") as f:
                for span in spans:
                    try:
                        f.write(
                            json.dumps(self._span_to_dict(span), default=str) + "\n"
                        )
                    except Exception:
                        pass
        return 0  # SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 0) -> bool:
        return True


class _GenAIEnrichingProcessor:
    """SpanProcessor that maps OpenInference attributes to GenAI semantic conventions.

    The OpenInference OpenAI instrumentor emits ``llm.*`` attributes, but
    Galileo's GenAI pattern detector requires ``gen_ai.*`` attributes to
    recognise spans.  This processor runs first in the chain and enriches
    each span's internal attribute dict so downstream processors (Galileo,
    Langfuse, file exporter) all see the standard GenAI conventions.

    Mapping:
        llm.system           → gen_ai.system
        llm.provider         → gen_ai.provider.name
        llm.model_name       → gen_ai.request.model
        embedding.model_name → gen_ai.request.model
        openinference.span.kind "LLM"       → gen_ai.operation.name "chat"
        openinference.span.kind "EMBEDDING"  → gen_ai.operation.name "embeddings"
    """

    _OI_TO_GENAI = {
        "llm.system": "gen_ai.system",
        "llm.provider": "gen_ai.provider.name",
        "llm.model_name": "gen_ai.request.model",
        "embedding.model_name": "gen_ai.request.model",
    }

    _KIND_TO_OP = {
        "LLM": "chat",
        "EMBEDDING": "embeddings",
        "CHAIN": "chain",
    }

    def on_start(self, span, parent_context=None) -> None:
        # Galileo SDK sets these on every span so the exporter can route them
        try:
            span.set_attribute(
                "galileo.project.name",
                os.getenv("GALILEO_PROJECT", "chronicle"),
            )
            span.set_attribute(
                "galileo.logstream.name",
                os.getenv("GALILEO_LOG_STREAM", "default"),
            )
        except Exception:
            pass

    def on_end(self, span) -> None:
        attrs = span.attributes
        if not attrs:
            return
        extra: Dict[str, Any] = {}
        for oi_key, genai_key in self._OI_TO_GENAI.items():
            if oi_key in attrs and genai_key not in attrs:
                extra[genai_key] = attrs[oi_key]

        oi_kind = attrs.get("openinference.span.kind", "")
        if oi_kind and "gen_ai.operation.name" not in attrs:
            op = self._KIND_TO_OP.get(oi_kind)
            if op:
                extra["gen_ai.operation.name"] = op

        if extra and hasattr(span, "_attributes") and span._attributes is not None:
            try:
                for k, v in extra.items():
                    span._attributes[k] = v
            except Exception:
                pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 0) -> bool:
        return True


def init_otel() -> None:
    """Initialize OTEL with configured exporters and OpenAI instrumentor.

    Supports multiple backends simultaneously:
    - Galileo: if GALILEO_API_KEY is set
    - Langfuse: if LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST are set

    Call once at app startup. No-op if no backends are configured.
    """
    galileo = is_galileo_enabled()
    langfuse = is_langfuse_enabled()
    debug_dir = _debug_otel_dir()

    if not galileo and not langfuse and not debug_dir:
        logger.info(
            "No OTEL backends configured (Galileo/Langfuse), skipping initialization"
        )
        return

    try:
        from opentelemetry.sdk import trace as trace_sdk
        from opentelemetry.sdk.resources import Resource

        global _tracer_provider
        resource = Resource.create(
            {
                "service.name": "chronicle",
                "galileo.project.name": os.getenv("GALILEO_PROJECT", "chronicle"),
                "galileo.logstream.name": os.getenv("GALILEO_LOG_STREAM", "default"),
            }
        )
        tracer_provider = trace_sdk.TracerProvider(resource=resource)
        _tracer_provider = tracer_provider
        backends = []

        # Set as global so all instrumentors and context-propagated spans use it
        from opentelemetry import trace as trace_api

        trace_api.set_tracer_provider(tracer_provider)

        # --- Attribute enricher (must be first for Galileo GenAI detection) ---
        tracer_provider.add_span_processor(_GenAIEnrichingProcessor())

        # --- Galileo backend (direct OTLP, matching working test setup) ---
        if galileo:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as _OTLPExporter,
                )
                from opentelemetry.sdk.trace.export import SimpleSpanProcessor

                galileo_api_key = os.getenv("GALILEO_API_KEY", "")
                galileo_console = os.getenv(
                    "GALILEO_CONSOLE_URL", "https://console.galileo.ai"
                )
                galileo_api_url = galileo_console.replace("console.", "api.")
                galileo_endpoint = f"{galileo_api_url}/otel/v1/traces"

                galileo_exporter = _OTLPExporter(
                    endpoint=galileo_endpoint,
                    headers={
                        "Galileo-API-Key": galileo_api_key,
                        "Content-Type": "application/x-protobuf",
                    },
                )
                tracer_provider.add_span_processor(
                    SimpleSpanProcessor(galileo_exporter)
                )
                backends.append("Galileo")
                logger.info("Galileo OTLP endpoint: %s", galileo_endpoint)
            except ImportError:
                logger.warning(
                    "OTLP exporter not installed. "
                    "Ensure opentelemetry-exporter-otlp-proto-http is installed."
                )
            except Exception as e:
                logger.error(f"Failed to add Galileo span processor: {e}")

        # --- Langfuse backend (via OTLP export) ---
        if langfuse:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.sdk.trace.export import SimpleSpanProcessor

                langfuse_host = os.getenv("LANGFUSE_HOST", "")
                langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
                langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")

                # Langfuse v4 OTLP endpoint
                otlp_endpoint = f"{langfuse_host}/api/public/otel/v1/traces"
                auth_header = f"Basic {__import__('base64').b64encode(f'{langfuse_public_key}:{langfuse_secret_key}'.encode()).decode()}"

                langfuse_exporter = OTLPSpanExporter(
                    endpoint=otlp_endpoint,
                    headers={"Authorization": auth_header},
                )
                tracer_provider.add_span_processor(
                    SimpleSpanProcessor(langfuse_exporter)
                )
                backends.append("Langfuse")
            except ImportError:
                logger.warning(
                    "OTLP exporter not installed. "
                    "Ensure opentelemetry-exporter-otlp-proto-http is installed."
                )
            except Exception as e:
                logger.error(f"Failed to add Langfuse span processor: {e}")

        # --- Debug file exporter ---
        if debug_dir:
            try:
                from opentelemetry.sdk.trace.export import SimpleSpanProcessor

                file_exporter = _FileSpanExporter(debug_dir)
                tracer_provider.add_span_processor(SimpleSpanProcessor(file_exporter))
                backends.append(f"File({debug_dir})")
            except Exception as e:
                logger.error(f"Failed to add debug file exporter: {e}")

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
            "OTEL SDK packages not installed. "
            "Install opentelemetry-api and opentelemetry-sdk."
        )
    except Exception as e:
        logger.error(f"Failed to initialize OTEL: {e}")


# ---------------------------------------------------------------------------
# Attribute mapping & tracing helpers
# ---------------------------------------------------------------------------

_ATTR_MAP: Dict[str, str] = {
    "user_id": "chronicle.user_id",
    "client_id": "chronicle.client_id",
    "pipeline_stage": "chronicle.pipeline.stage",
    "gen_ai_operation": "gen_ai.operation.name",
    "gen_ai_provider": "gen_ai.provider.name",
    "gen_ai_model": "gen_ai.request.model",
    "conversation_id": "gen_ai.conversation.id",
    "asr_hot_words": "chronicle.asr.hot_words",
    "asr_user_jargon": "chronicle.asr.user_jargon",
    "asr_context_length": "chronicle.asr.context_length",
}


def _map_attrs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Map keyword arguments to namespaced OTEL attribute names.

    Emits vendor-neutral GenAI/chronicle attributes as primary, plus
    Langfuse-specific duplicates for ``user_id`` so the Langfuse UI
    can populate its top-level User field.
    """
    mapped: Dict[str, Any] = {}
    for key, value in kwargs.items():
        otel_key = _ATTR_MAP.get(key)
        if otel_key and value is not None:
            mapped[otel_key] = value
        elif value is not None:
            # Pass through unknown keys with chronicle prefix
            mapped[f"chronicle.{key}"] = value

    # Dual-write for Langfuse UI field mapping
    if "chronicle.user_id" in mapped:
        mapped["langfuse.user.id"] = mapped["chronicle.user_id"]
    return mapped


def get_tracer(name: str = "chronicle"):
    """Return a tracer from the stored provider, or a no-op tracer."""
    if not _otel_initialised or _tracer_provider is None:
        try:
            from opentelemetry.trace import NoOpTracer

            return NoOpTracer()
        except ImportError:
            return None
    return _tracer_provider.get_tracer(name)


def set_span_attrs(**kwargs: Any) -> None:
    """Set attributes on the current active span.

    Uses the same attribute mapping as ``traced_job``.
    No-op if OTEL is not initialised or there is no active span.

    Example::

        set_span_attrs(user_id="abc", pipeline_stage="memory_extraction")
    """
    if not _otel_initialised:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        for key, value in _map_attrs(kwargs).items():
            span.set_attribute(key, value)
    except Exception:
        pass  # Never fail the caller


def set_trace_io(
    input: Any = None,
    output: Any = None,
) -> None:
    """Set input/output on the current span for Langfuse trace display.

    Langfuse reads ``langfuse.trace.input`` / ``langfuse.trace.output`` on the
    root span to populate the trace-level Input and Output tabs.  For child
    observations it reads ``langfuse.observation.input`` / ``…output`` (the
    OpenInference instrumentor already handles this for ChatCompletion spans
    via ``input.value`` / ``output.value``).

    Values are JSON-serialised if they are not already strings.

    Call this from within a ``@traced_job``-decorated function to set the
    trace-level (root span) input/output that appears in the Langfuse UI.

    Example::

        set_trace_io(
            input={"transcript": transcript_text[:500]},
            output={"memories_created": 5, "memory_ids": [...]},
        )
    """
    if not _otel_initialised:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return

        if input is not None:
            val = input if isinstance(input, str) else json.dumps(input, default=str)
            span.set_attribute("langfuse.trace.input", val)
        if output is not None:
            val = output if isinstance(output, str) else json.dumps(output, default=str)
            span.set_attribute("langfuse.trace.output", val)
    except Exception:
        pass  # Never fail the caller


def traced_job(name: str, **static_attrs: Any):
    """Decorator that wraps an async function in an OTEL span.

    Automatically reads ``conversation_id`` from the first positional arg
    (all RQ pipeline jobs follow this pattern). Sets static attributes
    immediately; use ``set_span_attrs()`` to enrich later.

    Example::

        @async_job(redis=True, beanie=True)
        @traced_job("memory_extraction", pipeline_stage="memory_extraction")
        async def process_memory_job(conversation_id, *, redis_client=None):
            ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if not _otel_initialised:
                return await func(*args, **kwargs)

            tracer = get_tracer()
            if tracer is None:
                return await func(*args, **kwargs)

            try:
                from opentelemetry import trace

                # Extract conversation_id from first positional arg
                conv_id = args[0] if args else kwargs.get("conversation_id")
                attrs = _map_attrs(static_attrs)
                if conv_id:
                    attrs["gen_ai.conversation.id"] = str(conv_id)

                with tracer.start_as_current_span(name, attributes=attrs) as span:
                    return await func(*args, **kwargs)
            except ImportError:
                return await func(*args, **kwargs)

        return wrapper

    return decorator
