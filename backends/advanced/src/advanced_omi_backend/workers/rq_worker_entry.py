#!/usr/bin/env python3
"""
RQ Worker Entry Point with Logging Configuration.

This script configures Python logging before starting RQ workers,
ensuring that application-level logs from job functions are visible.
"""

import logging
import os
import sys

# Configure logging BEFORE importing any application modules
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)

logger = logging.getLogger(__name__)


def main():
    """Start RQ worker with proper logging configuration."""
    from redis import Redis
    from rq import Worker

    # Get Redis URL from environment
    redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

    # Get queue names from command line arguments
    queue_names = sys.argv[1:] if len(sys.argv) > 1 else ['transcription', 'memory', 'default']

    logger.info(f"🚀 Starting RQ worker for queues: {', '.join(queue_names)}")
    logger.info(f"📡 Redis URL: {redis_url}")

    # Create Redis connection
    redis_conn = Redis.from_url(redis_url)

    # Create and start worker
    worker = Worker(
        queue_names,
        connection=redis_conn,
        log_job_description=True
    )

    logger.info("✅ RQ worker ready")

    # This blocks until worker is stopped
    worker.work(logging_level='INFO')


if __name__ == "__main__":
    main()
