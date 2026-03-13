#!/usr/bin/env python3
"""
Worker Orchestrator

Main entrypoint for Chronicle worker orchestration system.
Replaces start-workers.sh bash script with Python-based orchestration.

Usage:
    python worker_orchestrator.py
    # Or via Docker: docker compose up workers

Environment Variables:
    REDIS_URL                    Redis connection URL (default: redis://localhost:6379/0)
    WORKER_CHECK_INTERVAL        Health check interval in seconds (default: 10)
    MIN_RQ_WORKERS               Minimum expected RQ workers (default: 6)
    WORKER_STARTUP_GRACE_PERIOD  Grace period before health checks (default: 30)
    WORKER_SHUTDOWN_TIMEOUT      Max wait for graceful shutdown (default: 30)
    LOG_LEVEL                    Logging level (default: INFO)
"""

import asyncio
import logging
import os
import signal
import socket
import sys
from typing import Optional

from redis import Redis
from rq import Worker

# Import orchestrator components
from src.advanced_omi_backend.workers.orchestrator import (
    HealthMonitor,
    OrchestratorConfig,
    ProcessManager,
    build_worker_definitions,
)

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)


class WorkerOrchestrator:
    """
    Main orchestrator that coordinates all components.

    Handles:
    - Startup sequence (Redis cleanup, worker startup)
    - Signal handling (SIGTERM, SIGINT)
    - Health monitoring
    - Graceful shutdown
    """

    def __init__(self):
        self.config: Optional[OrchestratorConfig] = None
        self.redis: Optional[Redis] = None
        self.process_manager: Optional[ProcessManager] = None
        self.health_monitor: Optional[HealthMonitor] = None
        self.shutdown_event = asyncio.Event()

    async def startup(self):
        """
        Startup sequence.

        1. Load configuration
        2. Connect to Redis
        3. Clean up stale worker registrations
        4. Build worker definitions
        5. Start all workers
        6. Setup signal handlers
        7. Start health monitor
        """
        logger.info("🚀 Starting Chronicle Worker Orchestrator...")

        # 1. Load configuration
        logger.info("Loading configuration...")
        self.config = OrchestratorConfig()
        logger.info(f"Redis URL: {self.config.redis_url}")
        logger.info(f"Check interval: {self.config.check_interval}s")
        logger.info(f"Min RQ workers: {self.config.min_rq_workers}")
        logger.info(f"Startup grace period: {self.config.startup_grace_period}s")

        # 2. Connect to Redis
        logger.info("Connecting to Redis...")
        self.redis = Redis.from_url(self.config.redis_url)
        try:
            self.redis.ping()
            logger.info("✅ Redis connection successful")
        except Exception as e:
            logger.error(f"❌ Failed to connect to Redis: {e}")
            raise

        # 3. Clean up stale worker registrations
        logger.info("🧹 Cleaning up stale worker registrations from Redis...")
        cleaned_count = self._cleanup_stale_workers()
        if cleaned_count > 0:
            logger.info(f"Cleaned up {cleaned_count} stale workers")
        else:
            logger.info("No stale workers to clean")

        # 4. Build worker definitions
        logger.info("Building worker definitions...")
        worker_definitions = build_worker_definitions()
        logger.info(f"Total enabled workers: {len(worker_definitions)}")

        # 5. Create process manager and start all workers
        logger.info("Starting all workers...")
        self.process_manager = ProcessManager(worker_definitions)
        success = self.process_manager.start_all()

        if not success:
            logger.error("❌ Some workers failed to start")
            raise RuntimeError("Worker startup failed")

        # Log worker status
        logger.info("✅ All workers started:")
        for worker in self.process_manager.get_all_workers():
            logger.info(
                f"  - {worker.name}: PID {worker.pid} "
                f"(queues: {', '.join(worker.definition.queues) if worker.definition.queues else 'stream consumer'})"
            )

        # 6. Setup signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(self._signal_handler(s))
            )

        logger.info("✅ Signal handlers configured (SIGTERM, SIGINT)")

        # 7. Start health monitor
        logger.info("Starting health monitor...")
        self.health_monitor = HealthMonitor(
            self.process_manager, self.config, self.redis
        )
        await self.health_monitor.start()
        logger.info("✅ Health monitor started")

        logger.info(
            "⏳ Workers running - health monitor will auto-restart failed workers"
        )

    def _cleanup_stale_workers(self) -> int:
        """
        Clean up stale worker registrations from Redis.

        This replicates the bash script's logic:
        - Only clean up workers from THIS hostname (pod-aware)
        - Use RQ's register_death() to properly clean up

        Returns:
            Number of workers cleaned up
        """
        try:
            hostname = socket.gethostname()
            workers = Worker.all(connection=self.redis)
            cleaned = 0

            for worker in workers:
                if worker.hostname == hostname:
                    worker.register_death()
                    cleaned += 1

            return cleaned

        except Exception as e:
            logger.warning(f"Failed to clean up stale workers: {e}")
            return 0

    async def _signal_handler(self, sig: signal.Signals):
        """Handle shutdown signals"""
        logger.info(f"Received signal: {sig.name}")
        self.shutdown_event.set()

    async def shutdown(self):
        """
        Graceful shutdown sequence.

        1. Stop health monitor
        2. Stop all workers
        3. Close Redis connection
        """
        logger.info("🛑 Initiating graceful shutdown...")

        # 1. Stop health monitor
        if self.health_monitor:
            await self.health_monitor.stop()

        # 2. Stop all workers
        if self.process_manager:
            logger.info("Stopping all workers...")
            self.process_manager.stop_all(timeout=self.config.shutdown_timeout)

        # 3. Close Redis connection
        if self.redis:
            logger.info("Closing Redis connection...")
            self.redis.close()

        logger.info("✅ All workers stopped gracefully")

    async def run(self):
        """Main run loop - wait for shutdown signal"""
        try:
            # Perform startup
            await self.startup()

            # Wait for shutdown signal
            await self.shutdown_event.wait()

        except Exception as e:
            logger.exception(f"❌ Orchestrator error: {e}")
            raise
        finally:
            # Always perform shutdown
            await self.shutdown()


async def main():
    """Main entrypoint"""
    orchestrator = WorkerOrchestrator()

    try:
        await orchestrator.run()
        sys.exit(0)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)

    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Ensure unbuffered output for Docker logs
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Run the orchestrator
    asyncio.run(main())
