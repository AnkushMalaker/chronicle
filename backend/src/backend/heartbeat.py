"""Liveness heartbeats for the worker fleet and custom stream consumers.

RQ workers register and heartbeat with RQ itself (observable via ``Worker.all()``),
but registrations can outlive their containers and must be freshness-checked. The
custom stream-consumer workers (``streaming-stt``, ``windowed-batch``,
``wakeword-dispatch``) are plain processes, where "process is alive" does NOT
prove the main loop is still turning. Each beats once per main-loop iteration;
the workers-container healthcheck (``worker_healthcheck.py``) flags a stale
heartbeat so a wedged-but-alive consumer stops reporting healthy.
"""

import json
import time
from typing import Any

HEARTBEAT_KEY_PREFIX = "worker:heartbeat:"

# Keep the key well beyond the staleness window: a restarted worker refreshes it,
# and a permanently-removed worker's key eventually expires on its own — but a
# wedge (loop stops beating) is detectable long before that.
HEARTBEAT_TTL_SECONDS = 3600

# The orchestrator owns this heartbeat. Unlike RQ registrations, it disappears or
# becomes stale when the entire workers container is absent, which lets the backend
# detect an outage that cannot be observed by the in-container self-healer.
FLEET_HEALTH_KEY = "worker:fleet:health"
FLEET_HEARTBEAT_TTL_SECONDS = 300
FLEET_HEARTBEAT_MAX_AGE_SECONDS = 30


def evaluate_fleet_health(
    raw: str | bytes | None,
    *,
    now: float | None = None,
    max_age_seconds: float = FLEET_HEARTBEAT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Decode an orchestrator heartbeat into a stable health result."""
    checked_at = time.time() if now is None else now
    if raw is None:
        return {
            "healthy": False,
            "status": "missing",
            "detail": "No worker fleet heartbeat has been published",
        }

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    try:
        payload = json.loads(raw)
        timestamp = float(payload["timestamp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "healthy": False,
            "status": "invalid",
            "detail": f"Worker fleet heartbeat is invalid: {exc}",
        }

    age_seconds = max(0.0, checked_at - timestamp)
    result = {**payload, "age_seconds": age_seconds}
    if age_seconds > max_age_seconds:
        result.update(
            healthy=False,
            status="stale",
            detail=(
                f"Worker fleet heartbeat is {age_seconds:.1f}s old "
                f"(maximum {max_age_seconds:.0f}s)"
            ),
        )
        return result

    status = str(payload.get("status") or "invalid")
    result["status"] = status
    result["healthy"] = status == "healthy"
    if not result["healthy"] and not result.get("detail"):
        result["detail"] = f"Worker fleet reported status '{status}'"
    return result


def is_rq_worker_fresh(worker: Any, *, now: float | None = None) -> bool:
    """Return whether an RQ registration has heartbeated within its own TTL."""
    last_heartbeat = getattr(worker, "last_heartbeat", None)
    if last_heartbeat is None:
        return False
    try:
        heartbeat_at = last_heartbeat.timestamp()
        worker_ttl = float(getattr(worker, "worker_ttl", 420))
    except (AttributeError, TypeError, ValueError):
        return False
    checked_at = time.time() if now is None else now
    return checked_at - heartbeat_at <= worker_ttl


async def beat(redis_client, worker_name: str) -> None:
    """Record that ``worker_name``'s main loop just iterated. Best-effort.

    A heartbeat write must never break the work loop, so all errors are
    swallowed — a transient Redis blip simply means a missed beat, and the
    staleness window is generous enough to tolerate that.
    """
    try:
        await redis_client.set(
            f"{HEARTBEAT_KEY_PREFIX}{worker_name}",
            str(time.time()),
            ex=HEARTBEAT_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001 - heartbeat is best-effort
        pass
