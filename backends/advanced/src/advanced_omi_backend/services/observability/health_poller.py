"""Service-health poller — records an event on each health transition.

Runs as a background task in the FastAPI process. Every ``POLL_INTERVAL_SECS`` it
samples three sources and records a :class:`SystemEvent` **only when state changes**,
so the feed shows *when* something broke or recovered rather than re-alarming every
tick:

1. **Host-managed services** (via the node agent — :func:`get_external_services`),
   which already carry the crash-loop signal (``health_detail`` like
   ``"crash loop: containers restarted N×"``). A service entering a bad state →
   ``error`` (``critical`` for a crash loop); returning to healthy → ``info``.
2. **Failed RQ jobs** — newly-failed jobs (hard crashes / timeouts) that the
   per-site soft-failure taps can't see, deduped by job id.
3. **Worker fleet heartbeat** — missing, stale, or unhealthy supervisor state.
4. **Config diagnostics** — new configuration *issues* (errors), forgotten when
   resolved so they re-alarm if they recur.

Last-known state lives in Redis so it survives a backend restart without
re-alarming a still-down service.
"""

import asyncio
import logging

from rq.job import Job

from advanced_omi_backend.controllers.queue_controller import (
    QUEUE_NAMES,
    get_queue,
    redis_conn,
)
from advanced_omi_backend.controllers.system_controller import (
    get_config_diagnostics,
    get_external_services,
)
from advanced_omi_backend.heartbeat import FLEET_HEALTH_KEY, evaluate_fleet_health
from advanced_omi_backend.redis_factory import create_async_redis
from advanced_omi_backend.services.observability.system_events import record_event

logger = logging.getLogger("observability.health_poller")

POLL_INTERVAL_SECS = 15
# Let services finish booting before the first sample (avoid "starting" noise).
INITIAL_DELAY_SECS = 20

# Redis keys for last-known state.
_HEALTH_KEY = "system:health:last"  # hash: "{node}/{service}" -> health
_SEEN_FAILED_KEY = "system:health:seen_failed_jobs"  # set of job ids
_CONFIG_SEEN_KEY = "system:health:config_issues"  # set of issue keys
_WORKER_HEALTH_FIELD = "internal/workers-fleet"
_SEEN_FAILED_TTL = 7 * 24 * 3600

_BAD_WORKER_STATES = {"missing", "stale", "invalid", "unhealthy"}


async def _poll_worker_fleet(redis, *, now: float | None = None) -> None:
    """Record worker fleet outage and recovery transitions."""
    result = evaluate_fleet_health(await redis.get(FLEET_HEALTH_KEY), now=now)
    health = result["status"]
    previous = await redis.hget(_HEALTH_KEY, _WORKER_HEALTH_FIELD)

    # A fresh orchestrator can legitimately be in its startup grace period. Keep a
    # prior outage active until it reports healthy, but do not alarm on startup.
    if health == "starting":
        if previous is None:
            await redis.hset(_HEALTH_KEY, _WORKER_HEALTH_FIELD, health)
        return

    if health == previous:
        return

    previous_bad = previous in _BAD_WORKER_STATES
    current_bad = health in _BAD_WORKER_STATES
    await redis.hset(_HEALTH_KEY, _WORKER_HEALTH_FIELD, health)

    metadata = {
        "health": health,
        "previous": previous,
        "heartbeat_age_seconds": round(result.get("age_seconds", 0), 1),
        "workers_total": result.get("workers_total"),
        "workers_alive": result.get("workers_alive"),
    }
    if current_bad and not previous_bad:
        await record_event(
            severity="critical",
            category="service",
            source="workers",
            title="Worker fleet unavailable",
            detail=(
                f"{result.get('detail')}. Audio persistence, speech detection, "
                "transcription, memory, and background jobs may not run."
            ),
            metadata=metadata,
        )
    elif previous_bad and health == "healthy":
        await record_event(
            severity="info",
            category="service",
            source="workers",
            title="Worker fleet recovered",
            detail=(
                f"The worker supervisor reports {result.get('workers_alive', 0)}/"
                f"{result.get('workers_total', 0)} child processes alive."
            ),
            metadata=metadata,
        )


def _bad_severity(health: str | None, detail: str) -> str | None:
    """Return the event severity for a bad health state, or None if it's fine."""
    if not health:
        return None
    if "crash loop" in detail.lower():
        return "critical"
    if health == "unhealthy":
        return "error"
    if health == "partial":
        return "warning"
    return None  # healthy / starting / stopped / unknown are not "bad"


async def _poll_external_services(redis) -> None:
    data = await get_external_services()
    if not data.get("available"):
        return  # agent unreachable/unconfigured → unknown, don't fabricate transitions

    for svc in data.get("services", []) or []:
        if not svc.get("enabled", True):
            continue
        name = svc.get("name") or "unknown"
        node = svc.get("node") or "local"
        key = f"{node}/{name}"
        health = svc.get("health") or "unknown"
        detail = svc.get("health_detail") or ""

        prev = await redis.hget(_HEALTH_KEY, key)
        if prev == health:
            continue
        await redis.hset(_HEALTH_KEY, key, health)

        bad = _bad_severity(health, detail)
        prev_bad = _bad_severity(prev, "") if prev else None
        where = "" if node == "local" else f" on {node}"

        if bad and not prev_bad:
            await record_event(
                severity=bad,
                category="service",
                source=name,
                title=f"Service '{name}' is {health}{where}",
                detail=detail or None,
                metadata={"node": node, "health": health, "previous": prev},
            )
        elif prev_bad and not bad and prev is not None:
            await record_event(
                severity="info",
                category="service",
                source=name,
                title=f"Service '{name}' recovered ({health}){where}",
                metadata={"node": node, "previous": prev},
            )


async def _poll_failed_jobs(redis) -> None:
    for qname in QUEUE_NAMES:
        try:
            queue = get_queue(qname)
            for job_id in queue.failed_job_registry.get_job_ids():
                if await redis.sismember(_SEEN_FAILED_KEY, job_id):
                    continue
                await redis.sadd(_SEEN_FAILED_KEY, job_id)
                try:
                    job = Job.fetch(job_id, connection=redis_conn)
                except Exception:
                    continue
                func_name = job.func_name.split(".")[-1] if job.func_name else "unknown"
                exc = str(job.exc_info) if job.exc_info else None
                kwargs = job.kwargs or {}
                meta = job.meta or {}
                await record_event(
                    severity="error",
                    category="job",
                    source=func_name,
                    title=f"Job '{func_name}' failed",
                    detail=exc[-1000:] if exc else None,
                    traceback=exc,
                    user_id=kwargs.get("user_id") or None,
                    client_id=meta.get("client_id") or None,
                    conversation_id=kwargs.get("conversation_id")
                    or meta.get("conversation_id")
                    or None,
                    metadata={"job_id": job_id, "queue": qname},
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed-job poll for queue %s failed: %s", qname, e)

    await redis.expire(_SEEN_FAILED_KEY, _SEEN_FAILED_TTL)


async def _poll_config_diagnostics(redis) -> None:
    diag = await get_config_diagnostics()
    issues = diag.get("issues", []) or []

    current: set[str] = set()
    for issue in issues:
        component = issue.get("component", "config")
        message = issue.get("message", "")
        ikey = f"{component}:{message}"[:300]
        current.add(ikey)
        if await redis.sismember(_CONFIG_SEEN_KEY, ikey):
            continue
        await redis.sadd(_CONFIG_SEEN_KEY, ikey)
        await record_event(
            severity="error" if issue.get("severity") == "error" else "warning",
            category="config",
            source=component,
            title=message[:200] or "Configuration issue",
            detail=issue.get("resolution"),
            metadata={"overall_status": diag.get("overall_status")},
        )

    # Forget issues that have been resolved so they alarm again if they recur.
    stored = await redis.smembers(_CONFIG_SEEN_KEY)
    stale = set(stored) - current
    if stale:
        await redis.srem(_CONFIG_SEEN_KEY, *stale)


async def run_health_poller(app=None) -> None:
    """Periodic health-transition sampler. Cancelled on app shutdown."""
    redis = create_async_redis(decode_responses=True)
    logger.info("🩺 Health poller started (every %ss)", POLL_INTERVAL_SECS)
    try:
        await asyncio.sleep(INITIAL_DELAY_SECS)
        while True:
            try:
                await _poll_external_services(redis)
                await _poll_worker_fleet(redis)
                await _poll_failed_jobs(redis)
                await _poll_config_diagnostics(redis)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("Health poller pass failed: %s", e)
            await asyncio.sleep(POLL_INTERVAL_SECS)
    except asyncio.CancelledError:
        logger.info("🩺 Health poller stopped")
        raise
    finally:
        await redis.aclose()
