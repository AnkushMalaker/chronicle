"""Failure callback for the post-conversation RQ chain.

This is the *visibility* half of the event-driven recovery design. RQ's
``StartedJobRegistry.cleanup()`` reaps jobs whose worker died (``AbandonedJobError``)
and runs each job's ``on_failure`` callback before retrying it or — once retries are
exhausted — marking it FAILED and promoting its dependents. We hook that callback so
every chain failure surfaces as a visible ``system_event`` (instead of a job silently
sitting ``deferred`` forever) and leaves a diagnostic breadcrumb on the conversation.

Two hard constraints dictate how this is written (both were validated against the
installed RQ source):

1. **It runs in a *sync* context, in an *arbitrary* process.** ``cleanup()`` is called
   from the RQ worker's maintenance loop *and* from the FastAPI process whenever the
   Jobs page touches the registry (``len(registry)`` / ``get_job_ids()``). So it must
   not assume an event loop and must not assume Beanie is initialized — a Beanie call
   here would raise ``CollectionWasNotInitialized`` in whichever process didn't init
   it. Hence a standalone, process-agnostic ``pymongo.MongoClient`` and the sync,
   enqueue-only ``record_event_sync``.

2. **It must never raise.** ``Job.execute_failure_callback`` re-raises, and a throwing
   callback aborts the rest of the reaper's batch — skipping retry/promotion for every
   *other* abandoned job in the same sweep. So the whole body is wrapped in
   ``try/except: pass``.

The breadcrumb written here (``failure_stage``) is advisory only. ``processing_status``
remains solely owned by ``Conversation.apply_status`` at terminal points: because the
chain is wired with ``Dependency(allow_failure=True)``, the finalizer
(``dispatch_conversation_complete_event_job``) still runs after a mid-chain failure and
reconciles the real status — clearing this breadcrumb when the conversation actually
completed.
"""

import logging
import os
from typing import Optional

from rq.exceptions import AbandonedJobError

from advanced_omi_backend.services.observability.system_events import record_event_sync

logger = logging.getLogger(__name__)

# Lazily-created standalone sync client — deliberately NOT Beanie/Motor (see module
# docstring). Created on first failure, reused thereafter, scoped to this process.
_mongo_client = None
_conversations_col = None


def _get_conversations_col():
    global _mongo_client, _conversations_col
    if _conversations_col is None:
        from pymongo import MongoClient

        uri = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
        db_name = os.getenv("MONGODB_DATABASE", "chronicle")
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        _conversations_col = _mongo_client[db_name]["conversations"]
    return _conversations_col


def on_chain_job_failure(job, connection, exc_type, exc_value, exc_tb) -> None:
    """RQ ``on_failure`` hook for post-conversation chain jobs.

    Fired by ``execute_failure_callback`` both on a genuine raise inside the job and
    on abandonment (worker died → reaped by ``cleanup()``). Records a visible system
    event and writes an advisory ``failure_stage`` breadcrumb. Swallows everything.
    """
    try:
        meta = getattr(job, "meta", None) or {}
        stage: str = meta.get("failure_stage") or "post_conversation"
        conversation_id: Optional[str] = meta.get("conversation_id")
        abandoned = exc_type is not None and issubclass(exc_type, AbandonedJobError)

        # Advisory breadcrumb only — apply_status owns the real (status, stage) pair.
        if conversation_id:
            try:
                _get_conversations_col().update_one(
                    {"conversation_id": conversation_id},
                    {"$set": {"failure_stage": stage}},
                )
            except Exception:  # noqa: BLE001 — breadcrumb is best-effort
                pass

        retries_left = getattr(job, "retries_left", None)
        will_retry = bool(retries_left and retries_left > 0)
        if abandoned:
            title = f"{stage} job abandoned (worker died)"
            severity = "warning"
        else:
            title = f"{stage} job failed"
            severity = "error"
        if will_retry:
            title += f" — retrying ({retries_left} left)"

        detail = None
        if exc_value is not None:
            detail = f"{getattr(exc_type, '__name__', 'Error')}: {exc_value}"

        record_event_sync(
            severity=severity,
            category="pipeline",
            source=f"rq.{stage}",
            title=title,
            detail=detail,
            conversation_id=conversation_id,
            metadata={
                "job_id": getattr(job, "id", None),
                "stage": stage,
                "abandoned": abandoned,
                "retries_left": retries_left,
            },
        )
    except Exception:  # noqa: BLE001 — execute_failure_callback re-raises; never throw
        pass
