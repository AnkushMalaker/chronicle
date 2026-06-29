"""System event / error ledger.

A central, append-only store of operational and application failures across the
backend and its services: captured backend exceptions (every ``ERROR``/``CRITICAL``
log via :mod:`advanced_omi_backend.services.observability.log_handler`), semantic
failures tapped at high-value sites (WebSocket error-disconnects, streaming-ASR
terminal failures, failed/soft-failed jobs, plugin init failures),
service-health transitions (a sidecar entering/leaving a crash loop) detected by
the health poller, and errors pushed by sidecar services themselves over the
token-gated ``POST /api/admin/system-events/ingest`` endpoint (recorded with a
``service:<name>`` source).

It backs the admin-only "System Errors" page. Entries auto-expire after
``RETENTION_DAYS`` via a MongoDB TTL index on ``created_at`` — this is a rolling
window of "what broke recently", not a permanent audit trail.

Modeled on :class:`advanced_omi_backend.models.memory_audit.MemoryAuditEntry`.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from beanie import Document
from pydantic import Field
from pymongo import IndexModel

# How long events live before MongoDB's TTL monitor removes them.
RETENTION_DAYS = 30
_RETENTION_SECONDS = RETENTION_DAYS * 24 * 3600

# Canonical severities and categories. Stored as plain strings (not enums) so the
# catch-all log handler can record arbitrary backend errors without the schema
# fighting it; the WebUI filters on these values.
SEVERITIES = ("info", "warning", "error", "critical")
CATEGORIES = (
    "service",  # a service/container health transition (e.g. crash loop)
    "client",  # a device/WebSocket connection event
    "pipeline",  # audio/transcription pipeline failure
    "job",  # an RQ job failure (hard or soft)
    "plugin",  # plugin init/dispatch failure
    "config",  # configuration diagnostics issue
    "api",  # request-handling error
    "log",  # generic captured ERROR/CRITICAL log (catch-all net)
)


class SystemEvent(Document):
    """One recorded operational/application error or health transition."""

    severity: str = Field(description="info | warning | error | critical")
    category: str = Field(description="One of CATEGORIES; free-form fallback allowed")
    source: str = Field(
        description="Where it came from: service name, logger name, or client_id"
    )
    title: str = Field(description="Short one-line summary")
    detail: Optional[str] = Field(None, description="Longer message / context")
    traceback: Optional[str] = Field(None, description="Formatted traceback, if any")

    # Identity context (any may be absent depending on the source).
    user_id: Optional[str] = Field(None)
    client_id: Optional[str] = Field(None)
    conversation_id: Optional[str] = Field(None)

    # De-duplication: events with the same fingerprint that recur within a short
    # window collapse onto one row (``count`` incremented, ``last_seen_at`` bumped)
    # so a crash loop logging the same error every second doesn't flood the feed.
    fingerprint: str = Field(
        description="Stable hash of severity+category+source+title"
    )
    occurrences: int = Field(
        default=1, description="How many times this event has recurred (dedup count)"
    )
    last_seen_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this event most recently recurred",
    )

    acked: bool = Field(default=False, description="Whether an admin acknowledged it")
    metadata: Dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the event was first recorded",
    )

    class Settings:
        name = "system_events"
        indexes = [
            "fingerprint",
            "client_id",
            # Facet filters with newest-first sort.
            IndexModel(
                [("severity", 1), ("created_at", -1)], name="system_events_severity"
            ),
            IndexModel(
                [("category", 1), ("created_at", -1)], name="system_events_category"
            ),
            # TTL index for 30-day retention. A single-field index is direction-agnostic
            # for sorting, so this {created_at: 1} index also serves the plain
            # newest-first listing — no separate descending index needed.
            IndexModel(
                [("created_at", 1)],
                name="system_events_ttl",
                expireAfterSeconds=_RETENTION_SECONDS,
            ),
        ]
