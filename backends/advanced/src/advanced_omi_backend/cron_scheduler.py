"""
Config-driven asyncio cron scheduler for Chronicle.

Reads job definitions from config.yml ``cron_jobs`` section, uses ``croniter``
to compute next-run times, and dispatches registered job functions.  State
(last_run / next_run) is persisted in Redis so it survives restarts.

Usage:
    scheduler = get_scheduler()
    await scheduler.start()   # call during FastAPI lifespan startup
    await scheduler.stop()    # call during shutdown
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

import redis.asyncio as aioredis
from croniter import croniter

from advanced_omi_backend.config_loader import load_config, save_config_section

logger = logging.getLogger(__name__)

# Redis key prefixes
_LAST_RUN_KEY = "cron:last_run:{job_id}"
_NEXT_RUN_KEY = "cron:next_run:{job_id}"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CronJobConfig:
    job_id: str
    enabled: bool
    schedule: str
    description: str
    next_run: Optional[datetime] = None
    last_run: Optional[datetime] = None
    running: bool = False
    last_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Job registry – maps job_id → async callable
# ---------------------------------------------------------------------------

JobFunc = Callable[[], Coroutine[Any, Any, dict]]

_JOB_REGISTRY: Dict[str, JobFunc] = {}


def register_cron_job(job_id: str, func: JobFunc) -> None:
    """Register a job function so the scheduler can dispatch it."""
    _JOB_REGISTRY[job_id] = func


def _get_job_func(job_id: str) -> Optional[JobFunc]:
    return _JOB_REGISTRY.get(job_id)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class CronScheduler:
    def __init__(self) -> None:
        self.jobs: Dict[str, CronJobConfig] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._redis: Optional[aioredis.Redis] = None
        self._active_tasks: set[asyncio.Task] = set()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Load config, restore state from Redis, and start the scheduler loop."""
        import os
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

        self._load_jobs_from_config()
        await self._restore_state()

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Cron scheduler started with {len(self.jobs)} jobs")

    async def stop(self) -> None:
        """Cancel the scheduler loop and close Redis."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._redis:
            await self._redis.close()
        logger.info("Cron scheduler stopped")

    # -- public API ----------------------------------------------------------

    async def run_job_now(self, job_id: str) -> dict:
        """Manually trigger a job regardless of schedule."""
        if job_id not in self.jobs:
            raise ValueError(f"Unknown cron job: {job_id}")
        if self.jobs[job_id].running:
            return {"error": f"Job '{job_id}' is already running"}
        return await self._execute_job(job_id)

    async def update_job(
        self,
        job_id: str,
        enabled: Optional[bool] = None,
        schedule: Optional[str] = None,
    ) -> None:
        """Update a job's config and persist to config.yml."""
        if job_id not in self.jobs:
            raise ValueError(f"Unknown cron job: {job_id}")

        cfg = self.jobs[job_id]

        if schedule is not None:
            # Validate cron expression
            if not croniter.is_valid(schedule):
                raise ValueError(f"Invalid cron expression: {schedule}")
            cfg.schedule = schedule
            cfg.next_run = croniter(schedule, datetime.now(timezone.utc)).get_next(datetime)

        if enabled is not None:
            cfg.enabled = enabled

        # Persist changes to config.yml
        save_config_section(
            f"cron_jobs.{job_id}",
            {"enabled": cfg.enabled, "schedule": cfg.schedule, "description": cfg.description},
        )

        # Update next_run in Redis
        if self._redis and cfg.next_run:
            await self._redis.set(
                _NEXT_RUN_KEY.format(job_id=job_id),
                cfg.next_run.isoformat(),
            )

        logger.info(f"Updated cron job '{job_id}': enabled={cfg.enabled}, schedule={cfg.schedule}")

    async def get_all_jobs_status(self) -> List[dict]:
        """Return status of all registered cron jobs."""
        result = []
        for job_id, cfg in self.jobs.items():
            result.append({
                "job_id": job_id,
                "enabled": cfg.enabled,
                "schedule": cfg.schedule,
                "description": cfg.description,
                "last_run": cfg.last_run.isoformat() if cfg.last_run else None,
                "next_run": cfg.next_run.isoformat() if cfg.next_run else None,
                "running": cfg.running,
                "last_error": cfg.last_error,
            })
        return result

    # -- internals -----------------------------------------------------------

    def _load_jobs_from_config(self) -> None:
        """Read cron_jobs section from config.yml."""
        cfg = load_config()
        cron_section = cfg.get("cron_jobs", {})

        for job_id, job_cfg in cron_section.items():
            schedule = str(job_cfg.get("schedule", "0 * * * *"))
            if not croniter.is_valid(schedule):
                logger.warning(f"Invalid cron expression for job '{job_id}': {schedule} — skipping")
                continue
            now = datetime.now(timezone.utc)
            self.jobs[job_id] = CronJobConfig(
                job_id=job_id,
                enabled=bool(job_cfg.get("enabled", False)),
                schedule=schedule,
                description=str(job_cfg.get("description", "")),
                next_run=croniter(schedule, now).get_next(datetime),
            )

    async def _restore_state(self) -> None:
        """Restore last_run / next_run from Redis."""
        if not self._redis:
            return
        for job_id, cfg in self.jobs.items():
            try:
                lr = await self._redis.get(_LAST_RUN_KEY.format(job_id=job_id))
                if lr:
                    cfg.last_run = datetime.fromisoformat(lr)
                nr = await self._redis.get(_NEXT_RUN_KEY.format(job_id=job_id))
                if nr:
                    cfg.next_run = datetime.fromisoformat(nr)
            except Exception as e:
                logger.warning(f"Failed to restore state for job '{job_id}': {e}")

    async def _persist_state(self, job_id: str) -> None:
        """Write last_run / next_run to Redis."""
        if not self._redis:
            return
        cfg = self.jobs[job_id]
        try:
            if cfg.last_run:
                await self._redis.set(
                    _LAST_RUN_KEY.format(job_id=job_id),
                    cfg.last_run.isoformat(),
                )
            if cfg.next_run:
                await self._redis.set(
                    _NEXT_RUN_KEY.format(job_id=job_id),
                    cfg.next_run.isoformat(),
                )
        except Exception as e:
            logger.warning(f"Failed to persist state for job '{job_id}': {e}")

    async def _execute_job(self, job_id: str) -> dict:
        """Run the job function and update state."""
        cfg = self.jobs[job_id]
        func = _get_job_func(job_id)
        if func is None:
            msg = f"No function registered for cron job '{job_id}'"
            logger.error(msg)
            cfg.last_error = msg
            return {"error": msg}

        cfg.running = True
        cfg.last_error = None
        now = datetime.now(timezone.utc)
        logger.info(f"Executing cron job '{job_id}'")

        try:
            result = await func()
            cfg.last_run = now
            cfg.next_run = croniter(cfg.schedule, now).get_next(datetime)
            await self._persist_state(job_id)
            logger.info(f"Cron job '{job_id}' completed: {result}")
            return result or {}
        except Exception as e:
            cfg.last_error = str(e)
            logger.error(f"Cron job '{job_id}' failed: {e}", exc_info=True)
            # Still advance next_run so we don't spin on failures
            cfg.last_run = now
            cfg.next_run = croniter(cfg.schedule, now).get_next(datetime)
            await self._persist_state(job_id)
            return {"error": str(e)}
        finally:
            cfg.running = False

    async def _loop(self) -> None:
        """Main scheduler loop – checks every 30s for due jobs."""
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                for job_id, cfg in self.jobs.items():
                    if not cfg.enabled or cfg.running:
                        continue
                    if cfg.next_run and now >= cfg.next_run:
                        task = asyncio.create_task(self._execute_job(job_id))
                        self._active_tasks.add(task)
                        task.add_done_callback(self._active_tasks.discard)
            except Exception as e:
                logger.error(f"Error in cron scheduler loop: {e}", exc_info=True)
            await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_scheduler: Optional[CronScheduler] = None


def get_scheduler() -> CronScheduler:
    """Get (or create) the global CronScheduler singleton."""
    global _scheduler
    if _scheduler is None:
        _scheduler = CronScheduler()
    return _scheduler
