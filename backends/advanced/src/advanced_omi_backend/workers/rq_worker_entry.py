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
        from advanced_omi_backend.observability.otel_setup import init_otel

        init_otel()
    except Exception:
        pass  # Optional — don't block workers

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

    # Create and start worker
    worker = Worker(queue_names, connection=redis_conn, log_job_description=True)

    logger.info("✅ RQ worker ready")

    # This blocks until worker is stopped
    worker.work(logging_level="INFO")


if __name__ == "__main__":
    main()
