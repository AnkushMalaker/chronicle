#!/usr/bin/env python3
"""Container healthcheck for the ``workers`` service.

The workers container runs a process supervisor (``worker_orchestrator.py``) with
``restart: unless-stopped``, so Docker only notices total process exit. That
misses the cases that actually matter: the RQ worker fleet silently shrinking, or
a stream-consumer that's alive-but-wedged. This probe makes those visible by
failing (exit 1) when:

  1. fewer than ``MIN_RQ_WORKERS`` fresh RQ workers are registered in Redis, or
  2. any stream-consumer heartbeat (``worker:heartbeat:*``) is stale.

Used as the ``workers`` service healthcheck in docker-compose.yml.
"""

import os
import sys
import time

from redis import Redis

from backend.heartbeat import is_rq_worker_fresh

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
MIN_RQ_WORKERS = int(os.getenv("MIN_RQ_WORKERS", "6"))
# A stream-consumer beats once per ~1s main-loop iteration; 90s of silence means
# its loop has stopped turning (wedged) even though the process may still be up.
HEARTBEAT_MAX_AGE = int(os.getenv("WORKER_HEARTBEAT_MAX_AGE", "90"))
HEARTBEAT_PREFIX = "worker:heartbeat:"


def main() -> int:
    try:
        redis_client = Redis.from_url(REDIS_URL)
        redis_client.ping()
    except Exception as e:  # noqa: BLE001
        print(f"unhealthy: redis unreachable: {e}")
        return 1

    # 1. Fresh RQ worker registrations. Redis can retain registrations from dead
    #    containers, so Worker.all() alone is not a liveness signal.
    try:
        # Soft dependency: this healthcheck must still run without rq installed.
        from rq import Worker

        rq_count = sum(
            is_rq_worker_fresh(worker) for worker in Worker.all(connection=redis_client)
        )
    except Exception as e:  # noqa: BLE001
        print(f"unhealthy: could not count RQ workers: {e}")
        return 1

    if rq_count < MIN_RQ_WORKERS:
        print(f"unhealthy: only {rq_count}/{MIN_RQ_WORKERS} RQ workers registered")
        return 1

    # 2. Stream-consumer heartbeat staleness. Absent key => that worker isn't
    #    enabled (ignored); present-but-stale => its main loop wedged.
    now = time.time()
    stale = []
    for key in redis_client.scan_iter(match=f"{HEARTBEAT_PREFIX}*", count=100):
        value = redis_client.get(key)
        if value is None:
            continue
        try:
            age = now - float(value)
        except (TypeError, ValueError):
            continue
        if age > HEARTBEAT_MAX_AGE:
            name = key.decode() if isinstance(key, bytes) else key
            stale.append(f"{name[len(HEARTBEAT_PREFIX):]}({age:.0f}s)")

    if stale:
        print(f"unhealthy: stale stream-consumer heartbeats: {', '.join(stale)}")
        return 1

    print(f"ok: {rq_count} RQ workers registered, no stale heartbeats")
    return 0


if __name__ == "__main__":
    sys.exit(main())
