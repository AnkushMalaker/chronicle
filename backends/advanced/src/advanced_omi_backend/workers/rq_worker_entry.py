#!/usr/bin/env python3
"""
RQ Worker Entry Point with Logging Configuration.

This script configures Python logging before starting RQ workers,
ensuring that application-level logs from job functions are visible.
"""

import logging
import sys

# Configure logging BEFORE importing any application modules
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

# Catch-all: record every ERROR/CRITICAL log in this worker as a system event
# (enqueue-only; the FastAPI-process drain persists it).
try:
    from advanced_omi_backend.services.observability.log_handler import (
        install_system_event_log_handler,
    )

    install_system_event_log_handler()
except Exception:  # noqa: BLE001 — never block worker startup on observability
    pass


def main():
    """Start RQ worker with proper logging configuration."""
    # Initialize OTEL/Galileo if configured (patches OpenAI before any job imports)
    try:
        # Imported here, not at module level: this must run before any
        # application module is imported (see the comment above).
        from advanced_omi_backend.observability.otel_setup import init_otel

        init_otel()
    except Exception:
        pass  # Optional — don't block workers

    # Kept local (not hoisted): these must load AFTER init_otel() above, which
    # patches OpenAI/instrumentation before any application module is imported.
    from rq import Worker

    from advanced_omi_backend.redis_factory import REDIS_URL, create_sync_redis

    # Get queue names from command line arguments
    queue_names = (
        sys.argv[1:] if len(sys.argv) > 1 else ["transcription", "memory", "default"]
    )

    logger.info(f"🚀 Starting RQ worker for queues: {', '.join(queue_names)}")
    logger.info(f"📡 Redis URL: {REDIS_URL}")

    # Create Redis connection
    redis_conn = create_sync_redis()

    # Create and start worker.
    # maintenance_interval (default 600s) is how often the worker reaps abandoned jobs
    # (worker-died) from StartedJobRegistry → fires their on_failure callback + retries
    # / promotes dependents. Lowered to 120s so the event-driven recovery of a killed
    # job kicks in within ~2 min instead of ~10 (any process viewing the Jobs page also
    # triggers a reap immediately).
    worker = Worker(
        queue_names,
        connection=redis_conn,
        log_job_description=True,
        maintenance_interval=120,
    )

    logger.info("✅ RQ worker ready")

    # This blocks until worker is stopped.
    # with_scheduler: required for Retry(interval=...) — retried jobs land in
    # ScheduledJobRegistry and need a scheduler to promote them back onto the
    # queue (RQ elects one scheduler per queue via a lock, so this is safe
    # across multiple worker processes).
    worker.work(logging_level="INFO", with_scheduler=True)


if __name__ == "__main__":
    main()
