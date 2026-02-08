"""
Process Manager

Manages lifecycle of all worker processes with state tracking.
Handles process creation, monitoring, and graceful shutdown.
"""

import logging
import subprocess
import time
from enum import Enum
from typing import Dict, List, Optional

from .config import WorkerDefinition

logger = logging.getLogger(__name__)


class WorkerState(Enum):
    """Worker process lifecycle states"""

    PENDING = "pending"  # Not yet started
    STARTING = "starting"  # Process started, waiting for health check
    RUNNING = "running"  # Healthy and running
    UNHEALTHY = "unhealthy"  # Running but health check failed
    STOPPING = "stopping"  # Shutdown initiated
    STOPPED = "stopped"  # Cleanly stopped
    FAILED = "failed"  # Crashed or failed to start


class ManagedWorker:
    """
    Wraps a single worker process with state tracking.

    Attributes:
        definition: Worker definition
        process: Subprocess.Popen object (None if not started)
        state: Current worker state
        start_time: Timestamp when worker was started
        restart_count: Number of times worker has been restarted
        last_health_check: Timestamp of last health check
    """

    def __init__(self, definition: WorkerDefinition):
        self.definition = definition
        self.process: Optional[subprocess.Popen] = None
        self.state = WorkerState.PENDING
        self.start_time: Optional[float] = None
        self.restart_count = 0
        self.last_health_check: Optional[float] = None

    @property
    def name(self) -> str:
        """Worker name"""
        return self.definition.name

    @property
    def pid(self) -> Optional[int]:
        """Process ID (None if not started)"""
        return self.process.pid if self.process else None

    @property
    def is_alive(self) -> bool:
        """Check if process is alive"""
        if not self.process:
            return False
        return self.process.poll() is None

    def start(self) -> bool:
        """
        Start the worker process.

        Returns:
            True if started successfully, False otherwise
        """
        if self.process and self.is_alive:
            logger.warning(f"{self.name}: Already running (PID {self.pid})")
            return False

        try:
            logger.info(f"{self.name}: Starting worker...")
            logger.debug(f"{self.name}: Command: {' '.join(self.definition.command)}")

            # Don't capture stdout/stderr - let it flow to container logs (Docker captures it)
            # This prevents buffer overflow and blocking when worker output exceeds 64KB
            # Worker logs will be visible via 'docker logs' command
            self.process = subprocess.Popen(
                self.definition.command,
                stdout=None,  # Inherit from parent (goes to container stdout)
                stderr=None,  # Inherit from parent (goes to container stderr)
            )

            self.state = WorkerState.STARTING
            self.start_time = time.time()

            logger.info(f"{self.name}: Started with PID {self.pid}")
            return True

        except Exception as e:
            logger.error(f"{self.name}: Failed to start: {e}")
            self.state = WorkerState.FAILED
            return False

    def stop(self, timeout: int = 30) -> bool:
        """
        Gracefully stop the worker process.

        Args:
            timeout: Maximum wait time in seconds

        Returns:
            True if stopped successfully, False otherwise
        """
        if not self.process or not self.is_alive:
            logger.debug(f"{self.name}: Already stopped")
            self.state = WorkerState.STOPPED
            return True

        try:
            logger.info(f"{self.name}: Stopping worker (PID {self.pid})...")
            self.state = WorkerState.STOPPING

            # Send SIGTERM for graceful shutdown
            self.process.terminate()

            # Wait for process to exit
            try:
                self.process.wait(timeout=timeout)
                logger.info(f"{self.name}: Stopped gracefully")
                self.state = WorkerState.STOPPED
                return True

            except subprocess.TimeoutExpired:
                # Force kill if timeout exceeded
                logger.warning(
                    f"{self.name}: Timeout expired, force killing (SIGKILL)..."
                )
                self.process.kill()
                self.process.wait(timeout=5)
                logger.warning(f"{self.name}: Force killed")
                self.state = WorkerState.STOPPED
                return True

        except Exception as e:
            logger.error(f"{self.name}: Error during shutdown: {e}")
            self.state = WorkerState.FAILED
            return False

    def check_health(self) -> bool:
        """
        Check worker health.

        Returns:
            True if healthy, False otherwise
        """
        self.last_health_check = time.time()

        # Basic liveness check
        if not self.is_alive:
            logger.warning(f"{self.name}: Process is not alive")
            self.state = WorkerState.FAILED
            return False

        # Custom health check if defined
        if self.definition.health_check:
            try:
                if not self.definition.health_check():
                    logger.warning(f"{self.name}: Custom health check failed")
                    self.state = WorkerState.UNHEALTHY
                    return False
            except Exception as e:
                logger.error(f"{self.name}: Health check raised exception: {e}")
                self.state = WorkerState.UNHEALTHY
                return False

        # Update state if currently starting
        if self.state == WorkerState.STARTING:
            self.state = WorkerState.RUNNING

        return True


class ProcessManager:
    """
    Manages all worker processes.

    Provides high-level API for starting, stopping, and monitoring workers.
    """

    def __init__(self, worker_definitions: List[WorkerDefinition]):
        self.workers: Dict[str, ManagedWorker] = {
            defn.name: ManagedWorker(defn) for defn in worker_definitions
        }
        logger.info(f"ProcessManager initialized with {len(self.workers)} workers")

    def start_all(self) -> bool:
        """
        Start all workers.

        Returns:
            True if all workers started successfully
        """
        logger.info("Starting all workers...")
        success = True

        for worker in self.workers.values():
            if not worker.start():
                success = False

        if success:
            logger.info("All workers started successfully")
        else:
            logger.warning("Some workers failed to start")

        return success

    def stop_all(self, timeout: int = 30) -> bool:
        """
        Stop all workers gracefully.

        Args:
            timeout: Maximum wait time per worker in seconds

        Returns:
            True if all workers stopped successfully
        """
        logger.info("Stopping all workers...")
        success = True

        for worker in self.workers.values():
            if not worker.stop(timeout=timeout):
                success = False

        if success:
            logger.info("All workers stopped successfully")
        else:
            logger.warning("Some workers failed to stop cleanly")

        return success

    def restart_worker(self, name: str, timeout: int = 30) -> bool:
        """
        Restart a specific worker with timing measurements.

        Args:
            name: Worker name
            timeout: Maximum wait time for shutdown in seconds

        Returns:
            True if restarted successfully
        """
        worker = self.workers.get(name)
        if not worker:
            logger.error(f"Worker '{name}' not found")
            return False

        restart_start = time.time()
        logger.info(f"{name}: Starting restart at {time.strftime('%H:%M:%S')}")

        # STOP phase with timing
        stop_start = time.time()
        stop_success = worker.stop(timeout=timeout)
        stop_duration = time.time() - stop_start

        if not stop_success:
            logger.error(
                f"{name}: Failed to stop cleanly after {stop_duration:.2f}s "
                f"(timeout was {timeout}s), restart aborted"
            )
            worker.state = WorkerState.FAILED
            return False

        logger.info(
            f"{name}: Stopped in {stop_duration:.2f}s (timeout was {timeout}s)"
        )

        # START phase with timing
        start_start = time.time()
        success = worker.start()
        start_duration = time.time() - start_start

        total_restart_time = time.time() - restart_start

        if success:
            worker.restart_count += 1
            logger.info(
                f"{name}: Restart #{worker.restart_count} successful "
                f"(stop: {stop_duration:.2f}s, start: {start_duration:.2f}s, total: {total_restart_time:.2f}s)"
            )
        else:
            logger.error(
                f"{name}: Restart failed after {total_restart_time:.2f}s "
                f"(stop: {stop_duration:.2f}s, start attempt: {start_duration:.2f}s)"
            )

        return success

    def get_status(self) -> Dict[str, Dict]:
        """
        Get detailed status of all workers.

        Returns:
            Dictionary mapping worker name to status info
        """
        status = {}

        for name, worker in self.workers.items():
            status[name] = {
                "pid": worker.pid,
                "state": worker.state.value,
                "is_alive": worker.is_alive,
                "restart_count": worker.restart_count,
                "start_time": worker.start_time,
                "last_health_check": worker.last_health_check,
                "queues": worker.definition.queues,
            }

        return status

    def get_worker(self, name: str) -> Optional[ManagedWorker]:
        """Get worker by name"""
        return self.workers.get(name)

    def get_all_workers(self) -> List[ManagedWorker]:
        """Get all workers"""
        return list(self.workers.values())
