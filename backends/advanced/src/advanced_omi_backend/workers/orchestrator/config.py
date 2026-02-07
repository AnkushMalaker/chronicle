"""
Worker Orchestrator Configuration

Defines data structures for worker definitions and orchestrator configuration.
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional


class WorkerType(Enum):
    """Type of worker process"""

    RQ_WORKER = "rq_worker"  # RQ queue worker
    STREAM_CONSUMER = "stream_consumer"  # Redis Streams consumer


@dataclass
class WorkerDefinition:
    """
    Definition of a single worker process.

    Attributes:
        name: Unique identifier for the worker
        command: Full command to execute (as list for subprocess)
        worker_type: Type of worker (RQ vs stream consumer)
        queues: Queue names for RQ workers (empty for stream consumers)
        enabled_check: Optional predicate function to determine if worker should start
        restart_on_failure: Whether to automatically restart on failure
        health_check: Optional custom health check function
    """

    name: str
    command: List[str]
    worker_type: WorkerType = WorkerType.RQ_WORKER
    queues: List[str] = field(default_factory=list)
    enabled_check: Optional[Callable[[], bool]] = None
    restart_on_failure: bool = True
    health_check: Optional[Callable[[], bool]] = None

    def is_enabled(self) -> bool:
        """Check if this worker should be started"""
        if self.enabled_check is None:
            return True
        return self.enabled_check()


@dataclass
class OrchestratorConfig:
    """
    Global configuration for the worker orchestrator.

    All settings can be overridden via environment variables.
    """

    # Redis connection
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )

    # Health monitoring settings
    check_interval: int = field(
        default_factory=lambda: int(os.getenv("WORKER_CHECK_INTERVAL", "10"))
    )
    min_rq_workers: int = field(
        default_factory=lambda: int(os.getenv("MIN_RQ_WORKERS", "6"))
    )
    startup_grace_period: int = field(
        default_factory=lambda: int(os.getenv("WORKER_STARTUP_GRACE_PERIOD", "30"))
    )

    # Shutdown settings
    shutdown_timeout: int = field(
        default_factory=lambda: int(os.getenv("WORKER_SHUTDOWN_TIMEOUT", "30"))
    )

    # Logging
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self):
        """Validate configuration after initialization"""
        if self.check_interval <= 0:
            raise ValueError("check_interval must be positive")
        if self.min_rq_workers < 0:
            raise ValueError("min_rq_workers must be non-negative")
        if self.startup_grace_period < 0:
            raise ValueError("startup_grace_period must be non-negative")
        if self.shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be positive")
