"""
Worker Orchestrator Package

This package provides a Python-based orchestration system for managing
Chronicle's worker processes, replacing the bash-based start-workers.sh script.

Components:
- config: Worker definitions and orchestrator configuration
- worker_registry: Build worker list with conditional logic
- process_manager: Process lifecycle management
- health_monitor: Health checks and self-healing
"""

from .config import OrchestratorConfig, WorkerDefinition, WorkerType
from .health_monitor import HealthMonitor
from .process_manager import ManagedWorker, ProcessManager, WorkerState
from .worker_registry import build_worker_definitions

__all__ = [
    "WorkerDefinition",
    "OrchestratorConfig",
    "WorkerType",
    "build_worker_definitions",
    "ManagedWorker",
    "ProcessManager",
    "WorkerState",
    "HealthMonitor",
]
