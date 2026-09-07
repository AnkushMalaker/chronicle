"""Observability: a central system-event/error ledger and the machinery that fills it.

- :mod:`system_events` — the ``record_event`` / ``record_event_sync`` recorders, the
  SSE fan-out, and the async ingest drain.
- :mod:`log_handler` — a logging handler that turns every ``ERROR``/``CRITICAL`` log
  into a system event (the catch-all net).
- :mod:`health_poller` — polls service health and emits an event on each transition
  (e.g. a sidecar entering a crash loop).
"""

from backend.services.observability.system_events import (
    record_event,
    record_event_sync,
    run_event_ingest_drain,
)

__all__ = ["record_event", "record_event_sync", "run_event_ingest_drain"]
