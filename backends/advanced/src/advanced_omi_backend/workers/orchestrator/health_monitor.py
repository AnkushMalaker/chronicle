"""
Health Monitor

Self-healing monitor that detects and recovers from worker failures.
Periodically checks worker health and restarts failed workers.
"""

import asyncio
import logging
import time
from typing import Optional

from redis import Redis
from rq import Worker

from .config import OrchestratorConfig, WorkerType
from .process_manager import ProcessManager, WorkerState

logger = logging.getLogger(__name__)


class HealthMonitor:
    """
    Self-healing monitor for worker processes.

    Periodically checks:
    1. Individual worker health (process liveness)
    2. RQ worker registration count in Redis

    Automatically restarts failed workers if configured.
    """

    def __init__(
        self,
        process_manager: ProcessManager,
        config: OrchestratorConfig,
        redis_client: Redis,
    ):
        self.process_manager = process_manager
        self.config = config
        self.redis = redis_client
        self.running = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.start_time = time.time()
        self.last_registration_recovery: Optional[float] = None
        self.registration_recovery_cooldown = 60  # seconds

    async def start(self):
        """Start the health monitoring loop"""
        if self.running:
            logger.warning("Health monitor already running")
            return

        self.running = True
        self.start_time = time.time()
        logger.info(
            f"Starting health monitor (check interval: {self.config.check_interval}s, "
            f"grace period: {self.config.startup_grace_period}s)"
        )

        self.monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """Stop the health monitoring loop"""
        if not self.running:
            return

        logger.info("Stopping health monitor...")
        self.running = False

        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

        logger.info("Health monitor stopped")

    async def _monitor_loop(self):
        """Main monitoring loop"""
        try:
            while self.running:
                # Wait for startup grace period before starting checks
                elapsed = time.time() - self.start_time
                if elapsed < self.config.startup_grace_period:
                    remaining = self.config.startup_grace_period - elapsed
                    logger.debug(
                        f"In startup grace period - waiting {remaining:.0f}s before health checks"
                    )
                    await asyncio.sleep(self.config.check_interval)
                    continue

                # Perform health checks
                await self._check_health()

                # Wait for next check
                await asyncio.sleep(self.config.check_interval)

        except asyncio.CancelledError:
            logger.info("Health monitor loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Health monitor loop error: {e}", exc_info=True)
            self.running = False  # Mark monitor as stopped so callers know it's not active
            raise  # Re-raise to ensure the monitor task fails properly

    async def _check_health(self):
        """Perform all health checks and restart failed workers"""
        try:
            # Check individual worker health
            worker_health = self._check_worker_health()

            # Check RQ worker registration count
            rq_health = self._check_rq_worker_registration()

            # If RQ workers lost registration, trigger bulk restart (matches old bash script behavior)
            if not rq_health:
                self._handle_registration_loss()

            # Restart failed workers
            self._restart_failed_workers()

            # Log summary
            if not worker_health or not rq_health:
                logger.warning(
                    f"Health check: worker_health={worker_health}, rq_health={rq_health}"
                )

        except Exception as e:
            logger.error(f"Error during health check: {e}", exc_info=True)

    def _check_worker_health(self) -> bool:
        """
        Check individual worker health.

        Returns:
            True if all workers are healthy
        """
        all_healthy = True

        for worker in self.process_manager.get_all_workers():
            try:
                is_healthy = worker.check_health()
                if not is_healthy:
                    all_healthy = False
                    logger.warning(
                        f"{worker.name}: Health check failed (state={worker.state.value})"
                    )
            except Exception as e:
                all_healthy = False
                logger.error(f"{worker.name}: Health check raised exception: {e}")

        return all_healthy

    def _check_rq_worker_registration(self) -> bool:
        """
        Check RQ worker registration count in Redis.

        This replicates the bash script's logic:
        - Query Redis for all registered RQ workers
        - Check if count >= min_rq_workers

        Returns:
            True if RQ worker count is sufficient
        """
        try:
            workers = Worker.all(connection=self.redis)
            worker_count = len(workers)

            if worker_count < self.config.min_rq_workers:
                logger.warning(
                    f"RQ worker registration: {worker_count} workers "
                    f"(expected >= {self.config.min_rq_workers})"
                )
                return False

            logger.debug(f"RQ worker registration: {worker_count} workers registered")
            return True

        except Exception as e:
            logger.error(f"Failed to check RQ worker registration: {e}")
            return False

    def _restart_failed_workers(self):
        """Restart workers that have failed and should be restarted"""
        for worker in self.process_manager.get_all_workers():
            # Only restart if:
            # 1. Worker state is FAILED
            # 2. Worker definition has restart_on_failure=True
            if (
                worker.state == WorkerState.FAILED
                and worker.definition.restart_on_failure
            ):
                logger.warning(
                    f"{worker.name}: Worker failed, initiating restart "
                    f"(restart count: {worker.restart_count})"
                )

                success = self.process_manager.restart_worker(worker.name)

                if success:
                    logger.info(
                        f"{worker.name}: Restart successful "
                        f"(total restarts: {worker.restart_count})"
                    )
                else:
                    logger.error(f"{worker.name}: Restart failed")

    def _handle_registration_loss(self):
        """
        Handle RQ worker registration loss.

        This replicates the old bash script's self-healing behavior:
        - Check if cooldown period has passed
        - Restart all RQ workers (bulk restart)
        - Update recovery timestamp

        Cooldown prevents too-frequent restarts during Redis/network issues.
        """
        current_time = time.time()

        # Check if cooldown period has passed
        if self.last_registration_recovery is not None:
            elapsed = current_time - self.last_registration_recovery
            if elapsed < self.registration_recovery_cooldown:
                remaining = self.registration_recovery_cooldown - elapsed
                logger.debug(
                    f"Registration recovery cooldown active - "
                    f"waiting {remaining:.0f}s before next recovery attempt"
                )
                return

        logger.warning(
            "⚠️  RQ worker registration loss detected - initiating bulk restart "
            "(replicating old start-workers.sh behavior)"
        )

        # Restart all RQ workers (this method now handles timestamp update internally)
        success = self._restart_all_rq_workers()

        if success:
            logger.info("✅ Bulk restart completed - workers should re-register soon")
        else:
            logger.error("❌ Bulk restart encountered errors - check individual worker logs")

    def _restart_all_rq_workers(self) -> bool:
        """
        Restart all RQ workers (bulk restart) with timing measurements.

        This matches the old bash script's recovery mechanism:
        - Kill all RQ workers
        - Restart them
        - Workers will automatically re-register with Redis on startup

        Returns:
            True if all RQ workers restarted successfully, False otherwise
        """
        rq_workers = [
            worker
            for worker in self.process_manager.get_all_workers()
            if worker.definition.worker_type == WorkerType.RQ_WORKER
        ]

        if not rq_workers:
            logger.warning("No RQ workers found to restart")
            return False

        # START TIMING
        bulk_restart_start = time.time()
        logger.warning(
            f"⚠️  RQ worker registration lost! "
            f"Starting bulk restart of {len(rq_workers)} workers at {time.strftime('%H:%M:%S')}"
        )

        all_success = True
        worker_times = []  # Track individual worker restart times

        for i, worker in enumerate(rq_workers, 1):
            worker_start = time.time()
            logger.info(
                f"  [{i}/{len(rq_workers)}] ↻ Restarting {worker.name} at {time.strftime('%H:%M:%S')}..."
            )

            success = self.process_manager.restart_worker(worker.name)

            worker_duration = time.time() - worker_start
            worker_times.append((worker.name, worker_duration))

            if success:
                logger.info(
                    f"  [{i}/{len(rq_workers)}] ✓ {worker.name} restarted in {worker_duration:.2f}s"
                )
            else:
                logger.error(
                    f"  [{i}/{len(rq_workers)}] ✗ {worker.name} restart failed after {worker_duration:.2f}s"
                )
                all_success = False

        # END TIMING
        total_duration = time.time() - bulk_restart_start

        # Log timing summary
        logger.info(f"\n⏱️  Bulk Restart Timing Summary:")
        logger.info(f"  Total workers: {len(rq_workers)}")
        logger.info(
            f"  Total time: {total_duration:.2f}s ({total_duration/60:.1f} minutes)"
        )
        logger.info(f"  Average per worker: {total_duration/len(rq_workers):.2f}s")

        if worker_times:
            slowest = max(worker_times, key=lambda x: x[1])
            fastest = min(worker_times, key=lambda x: x[1])
            logger.info(f"  Slowest worker: {slowest[0]} ({slowest[1]:.2f}s)")
            logger.info(f"  Fastest worker: {fastest[0]} ({fastest[1]:.2f}s)")

        # Update recovery timestamp (moved here from _handle_registration_loss)
        self.last_registration_recovery = time.time()

        if all_success:
            logger.info(
                f"✅ Successfully restarted all {len(rq_workers)} RQ workers in {total_duration:.2f}s"
            )
        else:
            logger.warning(
                f"⚠️  Some workers failed to restart (took {total_duration:.2f}s total)"
            )

        return all_success

    def get_health_status(self) -> dict:
        """
        Get current health status summary.

        Returns:
            Dictionary with health status information
        """
        worker_status = self.process_manager.get_status()

        # Count workers by state
        state_counts = {}
        for status in worker_status.values():
            state = status["state"]
            state_counts[state] = state_counts.get(state, 0) + 1

        # Check RQ worker registration
        try:
            rq_workers = Worker.all(connection=self.redis)
            rq_worker_count = len(rq_workers)
        except Exception:
            rq_worker_count = -1  # Error indicator

        return {
            "running": self.running,
            "uptime": time.time() - self.start_time if self.running else 0,
            "total_workers": len(worker_status),
            "state_counts": state_counts,
            "rq_worker_count": rq_worker_count,
            "min_rq_workers": self.config.min_rq_workers,
            "rq_healthy": rq_worker_count >= self.config.min_rq_workers,
        }
