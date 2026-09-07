"""Background task manager for tracking and managing all async tasks.

This module provides centralized task management to prevent orphaned tasks
and ensure proper cleanup of all background operations.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class TaskInfo:
    """Information about a tracked task."""

    task: asyncio.Task
    name: str
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    completed_at: Optional[float] = None
    error: Optional[str] = None
    cancelled: bool = False


class BackgroundTaskManager:
    """Manages all background tasks in the application."""

    def __init__(self):
        self.tasks: Dict[str, TaskInfo] = {}
        self.completed_tasks: List[TaskInfo] = []
        self.max_completed_history = 1000  # Keep last N completed tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._shutdown = False

    async def start(self):
        """Start the task manager."""
        logger.info("Starting BackgroundTaskManager")
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def shutdown(self):
        """Shutdown the task manager and cancel all tasks."""
        logger.info("Shutting down BackgroundTaskManager")
        self._shutdown = True

        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Cancel all active tasks
        active_tasks = list(self.tasks.values())
        logger.info(f"Cancelling {len(active_tasks)} active tasks")

        for task_info in active_tasks:
            if not task_info.task.done():
                task_info.task.cancel()
                task_info.cancelled = True

        # Wait for all tasks to complete with timeout
        if active_tasks:
            tasks = [info.task for info in active_tasks]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=30.0
                )
            except asyncio.TimeoutError:
                logger.warning("Some tasks did not complete within shutdown timeout")

        logger.info("BackgroundTaskManager shutdown complete")

    def track_task(
        self, task: asyncio.Task, name: str, metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Track a background task and return the task ID."""
        if metadata is None:
            metadata = {}

        task_id = f"{name}_{id(task)}"
        task_info = TaskInfo(task=task, name=name, metadata=metadata)
        self.tasks[task_id] = task_info

        # Add done callback to track completion
        task.add_done_callback(lambda t: self._task_done(task_id))

        logger.debug(f"Tracking task: {name} (id: {task_id})")
        return task_id

    def _task_done(self, task_id: str):
        """Handle task completion."""
        if task_id not in self.tasks:
            return

        task_info = self.tasks[task_id]
        task_info.completed_at = time.time()

        # Check if task failed
        if task_info.task.cancelled():
            task_info.cancelled = True
            logger.debug(f"Task cancelled: {task_info.name}")
        elif task_info.task.exception():
            try:
                exc = task_info.task.exception()
                task_info.error = str(exc)
                logger.error(f"Task failed: {task_info.name} - {exc}")
            except Exception:
                task_info.error = "Unknown error"
        else:
            # DEBUG: Add more visible logging for memory task completion
            if "memory_" in task_info.name:
                logger.info(
                    f"✅ MEMORY TASK COMPLETED: {task_info.name} at {task_info.completed_at}"
                )
            else:
                logger.debug(f"Task completed: {task_info.name}")

        # Move to completed list
        del self.tasks[task_id]
        self.completed_tasks.append(task_info)

        # Trim completed history
        if len(self.completed_tasks) > self.max_completed_history:
            self.completed_tasks = self.completed_tasks[-self.max_completed_history :]

    async def _periodic_cleanup(self):
        """Periodically clean up completed tasks and check for timeouts."""
        logger.info("Started periodic task cleanup")

        try:
            while not self._shutdown:
                try:
                    # Wait 30 seconds between cleanups
                    await asyncio.sleep(30)

                    current_time = time.time()
                    timed_out_tasks = []

                    # Check for timed out tasks
                    for task_id, task_info in list(self.tasks.items()):
                        timeout = task_info.metadata.get("timeout")
                        if timeout:
                            age = current_time - task_info.created_at
                            if age > timeout and not task_info.task.done():
                                logger.warning(
                                    f"Task {task_info.name} exceeded timeout "
                                    f"({age:.1f}s > {timeout}s), cancelling"
                                )
                                task_info.task.cancel()
                                timed_out_tasks.append(task_info.name)

                    # Log statistics
                    active_count = len(self.tasks)
                    completed_count = len(self.completed_tasks)

                    if active_count > 0 or timed_out_tasks:
                        logger.info(
                            f"Task manager stats: {active_count} active, "
                            f"{completed_count} completed, "
                            f"{len(timed_out_tasks)} timed out"
                        )

                        # Log details of long-running tasks
                        long_running = []
                        for task_info in self.tasks.values():
                            age = current_time - task_info.created_at
                            if age > 60:  # Tasks older than 1 minute
                                long_running.append(f"{task_info.name} ({age:.0f}s)")

                        if long_running:
                            logger.info(
                                f"Long-running tasks: {', '.join(long_running[:5])}"
                            )

                except Exception as e:
                    logger.error(f"Error in periodic cleanup: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.info("Periodic cleanup cancelled")
        finally:
            logger.info("Periodic cleanup stopped")

    def get_active_tasks(self) -> List[TaskInfo]:
        """Get list of currently active tasks."""
        return list(self.tasks.values())

    def get_task_info(self, task_id: str) -> Optional[TaskInfo]:
        """Get task info by task ID from both active and completed tasks."""
        # First check active tasks
        task_info = self.tasks.get(task_id)
        if task_info:
            return task_info

        # Then check completed tasks
        for completed_task in self.completed_tasks:
            if f"{completed_task.name}_{id(completed_task.task)}" == task_id:
                return completed_task

        return None

    def get_task_count_by_type(self) -> Dict[str, int]:
        """Get count of active tasks grouped by type."""
        counts: Dict[str, int] = {}
        for task_info in self.tasks.values():
            task_type = task_info.metadata.get("type", "unknown")
            counts[task_type] = counts.get(task_type, 0) + 1
        return counts

    def get_tasks_for_client(self, client_id: str) -> List[TaskInfo]:
        """Get all active tasks for a specific client."""
        client_tasks = []
        for task_info in self.tasks.values():
            if task_info.metadata.get("client_id") == client_id:
                client_tasks.append(task_info)
        return client_tasks

    async def cancel_tasks_for_client(self, client_id: str, timeout: float = 30.0):
        """Cancel client-specific tasks, but preserve processing tasks that should continue independently."""
        client_tasks = self.get_tasks_for_client(client_id)
        if not client_tasks:
            return

        # Define task types that should continue after client disconnect
        # These tasks represent ongoing processing that should complete independently
        PROCESSING_TASK_TYPES = {
            "transcription_chunk",  # Individual transcription tasks
            "memory",  # Memory processing tasks
            "cropping",  # Audio cropping tasks
        }

        # Filter tasks to only cancel non-processing tasks
        tasks_to_cancel = []
        tasks_to_preserve = []

        for task_info in client_tasks:
            task_type = task_info.metadata.get("type", "")
            # Check if this is a processing task that should continue
            is_processing_task = any(
                task_type.startswith(pt) for pt in PROCESSING_TASK_TYPES
            )

            if is_processing_task:
                tasks_to_preserve.append(task_info)
            else:
                tasks_to_cancel.append(task_info)

        if tasks_to_preserve:
            logger.info(
                f"Preserving {len(tasks_to_preserve)} processing tasks for client {client_id} to continue independently"
            )
            for task_info in tasks_to_preserve:
                logger.debug(
                    f"  Preserving task: {task_info.name} (type: {task_info.metadata.get('type')})"
                )

        if not tasks_to_cancel:
            logger.info(f"No non-processing tasks to cancel for client {client_id}")
            return

        logger.info(
            f"Cancelling {len(tasks_to_cancel)} non-processing tasks for client {client_id}"
        )

        # Cancel only non-processing tasks
        for task_info in tasks_to_cancel:
            if not task_info.task.done():
                logger.debug(
                    f"  Cancelling task: {task_info.name} (type: {task_info.metadata.get('type')})"
                )
                task_info.task.cancel()
                task_info.cancelled = True

        # Wait for cancelled tasks to complete
        tasks = [info.task for info in tasks_to_cancel if not info.task.done()]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Some tasks for client {client_id} did not complete within timeout"
                )

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of the task manager."""
        current_time = time.time()
        active_tasks = self.get_active_tasks()

        # Calculate task ages
        task_ages = []
        oldest_task = None
        for task_info in active_tasks:
            age = current_time - task_info.created_at
            task_ages.append(age)
            if oldest_task is None or age > oldest_task[1]:
                oldest_task = (task_info.name, age)

        # Count errors in recent completed tasks
        recent_errors = 0
        recent_cancelled = 0
        for task_info in self.completed_tasks[-100:]:  # Last 100 tasks
            if task_info.error:
                recent_errors += 1
            elif task_info.cancelled:
                recent_cancelled += 1

        status = {
            "active_tasks": len(active_tasks),
            "completed_tasks": len(self.completed_tasks),
            "task_counts_by_type": self.get_task_count_by_type(),
            "oldest_task": oldest_task[0] if oldest_task else None,
            "oldest_task_age": oldest_task[1] if oldest_task else 0,
            "average_task_age": sum(task_ages) / len(task_ages) if task_ages else 0,
            "recent_errors": recent_errors,
            "recent_cancelled": recent_cancelled,
            "healthy": len(active_tasks) < 1000
            and (oldest_task[1] < 3600 if oldest_task else True),
        }

        return status


# Global task manager instance
_task_manager: Optional[BackgroundTaskManager] = None


def init_task_manager() -> BackgroundTaskManager:
    """Initialize the global task manager."""
    global _task_manager
    _task_manager = BackgroundTaskManager()
    return _task_manager


def get_task_manager() -> BackgroundTaskManager:
    """Get the global task manager instance."""
    if _task_manager is None:
        raise RuntimeError(
            "BackgroundTaskManager not initialized. Call init_task_manager first."
        )
    return _task_manager
