"""Liveness heartbeats for the custom stream-consumer workers.

RQ workers register and heartbeat with RQ itself (observable via ``Worker.all()``),
so a dead/wedged RQ worker drops out of the registration count. The custom
stream-consumer workers (``streaming-stt``, ``windowed-batch``,
``wakeword-dispatch``) are plain processes, where "process is alive" does NOT
prove the main loop is still turning. Each beats once per main-loop iteration;
the workers-container healthcheck (``worker_healthcheck.py``) flags a stale
heartbeat so a wedged-but-alive consumer stops reporting healthy.
"""

import time

HEARTBEAT_KEY_PREFIX = "worker:heartbeat:"

# Keep the key well beyond the staleness window: a restarted worker refreshes it,
# and a permanently-removed worker's key eventually expires on its own — but a
# wedge (loop stops beating) is detectable long before that.
HEARTBEAT_TTL_SECONDS = 3600


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
