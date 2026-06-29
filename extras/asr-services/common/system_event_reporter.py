"""Best-effort cross-service error reporter.

Ships this service's ``ERROR``/``CRITICAL`` log records to the Chronicle backend's
system-event ingest endpoint, so failures *inside* a sidecar (ASR, speaker-rec, …)
show up on the admin "System Errors" page alongside backend errors instead of being
buried in container logs.

It is the push half of the standard error-aggregation pattern (a mini-Sentry): the
backend exposes a token-gated ``POST /api/admin/system-events/ingest``; each service
attaches this handler to its root logger and POSTs error records to it.

Deliberately conservative:

* **Opt-in.** Does nothing unless both ``CHRONICLE_INGEST_URL`` and
  ``CHRONICLE_INGEST_TOKEN`` are set in the environment.
* **Non-blocking.** Records go onto a bounded in-memory queue drained by a single
  daemon thread; the logging call never waits on the network. If the queue is full
  (backend down / slow), new records are dropped, not buffered unbounded.
* **No third-party deps.** Uses ``urllib`` so it can drop into any service image.
* **Never raises.** A failure to report must not perturb the service. Feedback
  loops are avoided by skipping the reporter's own logger and the http stack.

Usage (once, at startup)::

    from common.system_event_reporter import install_system_event_reporter
    install_system_event_reporter(source="asr-vibevoice")
"""

import json
import logging
import os
import queue
import threading
import traceback as _traceback
import urllib.error
import urllib.request

# Loggers we must never forward — doing so could feed back into this reporter
# (an error while reporting an error would recurse).
_SKIP_PREFIXES = (
    "system_event_reporter",
    "urllib",
    "http",
    "asyncio",
)

# Cap pending records so a backend outage can't grow memory unbounded.
_QUEUE_MAX = 500


class _SystemEventReporter(logging.Handler):
    def __init__(self, url: str, token: str, source: str) -> None:
        super().__init__(level=logging.ERROR)
        self._url = url
        self._token = token
        self._source = source
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=_QUEUE_MAX)
        self._worker = threading.Thread(
            target=self._run, name="system-event-reporter", daemon=True
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            name = record.name or "root"
            if any(name.startswith(p) for p in _SKIP_PREFIXES):
                return

            severity = "critical" if record.levelno >= logging.CRITICAL else "error"
            message = record.getMessage()

            tb = None
            if record.exc_info:
                try:
                    tb = "".join(_traceback.format_exception(*record.exc_info))
                except Exception:
                    tb = None

            payload = {
                "severity": severity,
                "category": "service",
                "source": self._source,
                "title": message[:500],
                # Keep the full message when the title would truncate it.
                "detail": message if len(message) > 500 else None,
                "traceback": tb,
                "metadata": {
                    "logger": name,
                    "level": record.levelname,
                    "module": record.module,
                    "func": record.funcName,
                    "line": record.lineno,
                },
            }
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                pass  # drop rather than block the caller
        except Exception:  # noqa: BLE001 — a logging handler must never raise
            pass

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                self._post(payload)
            except Exception:  # noqa: BLE001 — best-effort
                pass

    def _post(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except urllib.error.URLError:
            # Backend unreachable or rejected the event — nothing we can do, and we
            # must not log at ERROR here (that would feed back into this handler).
            pass


_installed = False


def install_system_event_reporter(source: str) -> bool:
    """Attach the reporter to the root logger if configured. Idempotent.

    Returns True if installed (env configured), False if skipped.
    """
    global _installed
    if _installed:
        return True

    url = os.getenv("CHRONICLE_INGEST_URL", "").strip()
    token = os.getenv("CHRONICLE_INGEST_TOKEN", "").strip()
    source = (os.getenv("CHRONICLE_SERVICE_NAME") or source or "unknown").strip()
    if not url or not token:
        return False

    logging.getLogger().addHandler(_SystemEventReporter(url, token, source))
    _installed = True
    # Safe to log at INFO — this record won't be forwarded (below ERROR).
    logging.getLogger("system_event_reporter").info(
        "Cross-service error reporting enabled → %s (source=%s)", url, source
    )
    return True
