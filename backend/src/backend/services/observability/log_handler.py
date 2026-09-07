"""Catch-all logging handler that turns errors into system events.

Attached to the root logger in both the FastAPI process and the RQ worker process,
this captures every ``ERROR``/``CRITICAL`` log record across the backend and records
it as a :class:`~backend.models.system_event.SystemEvent` (via the
sync, enqueue-only :func:`record_event_sync`). This is the "something from
everywhere" net: it surfaces failures at sites that were never wired with an
explicit tap, without touching any of them.

It is deliberately conservative:

* Only ``levelno >= ERROR`` is captured.
* A skip-set of logger-name prefixes prevents feedback loops (the recorder, the SSE
  publisher, and the Redis/HTTP client libraries they use must never be captured —
  otherwise an error while recording an error would recurse).
* ``emit`` swallows every exception: a logging handler must never raise.
"""

import logging
import traceback as _traceback

from backend.services.observability.system_events import record_event_sync

# Loggers we must never capture — recording an event uses these, so capturing their
# errors would feed back into the recorder.
_SKIP_PREFIXES = (
    "observability",
    "backend.services.observability",
    "sse_publisher",
    "backend.services.sse_publisher",
    "redis",
    "urllib3",
    "httpcore",
)

# Coarse category from logger name; everything else is the generic "log" bucket.
_CATEGORY_HINTS = (
    ("audio_processing", "pipeline"),
    ("transcription", "pipeline"),
    ("api.requests", "api"),
    ("plugin", "plugin"),
)


def _category_for(name: str) -> str:
    for hint, category in _CATEGORY_HINTS:
        if hint in name:
            return category
    return "log"


class SystemEventLogHandler(logging.Handler):
    """A logging handler that records ERROR/CRITICAL logs as system events."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            name = record.name or "root"
            if any(name.startswith(prefix) for prefix in _SKIP_PREFIXES):
                return

            severity = "critical" if record.levelno >= logging.CRITICAL else "error"
            message = record.getMessage()

            tb = None
            if record.exc_info:
                try:
                    tb = "".join(_traceback.format_exception(*record.exc_info))
                except Exception:
                    tb = None

            # Keep the full message in `detail` whenever it would be lost in the
            # row's (single-line, truncated) title — i.e. when it's long or
            # multi-line — so the expanded view shows the complete, formatted text.
            detail = message if (len(message) > 200 or "\n" in message) else None

            record_event_sync(
                severity=severity,
                category=_category_for(name),
                source=name,
                title=message[:200],
                detail=detail,
                traceback=tb,
                metadata={
                    "level": record.levelname,
                    "module": record.module,
                    "func": record.funcName,
                    "line": record.lineno,
                },
            )
        except Exception:  # noqa: BLE001 — a handler must never raise
            pass


_installed = False


def install_system_event_log_handler() -> None:
    """Attach the handler to the root logger once (idempotent)."""
    global _installed
    if _installed:
        return
    logging.getLogger().addHandler(SystemEventLogHandler())
    _installed = True
