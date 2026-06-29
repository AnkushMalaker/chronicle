"""Background reaper: a single periodic loop that backstops several cleanups.

Each sweep here is a *belt-and-suspenders* backstop for a primary mechanism that
should already have done the work — the reaper only catches what slipped through:

1. **Stale clients** — a ``ClientState`` whose handler wedged and never tripped
   the WebSocket idle read-timeout, reaped by ``last_activity`` age so the Network
   page can't show an immortal "connected" zombie.
2. **Orphaned audio streams** — per-client ``audio:stream:{client_id}`` keys left
   behind when connection cleanup never ran, expired so their consumer groups are
   released.
3. **Orphaned deferred jobs** — RQ chain jobs stuck ``deferred`` forever because a
   dependency was hard-deleted (no completion/failure event ever fires to promote
   them, and ``Dependency(allow_failure=True)`` only covers a dependency that
   *fails*, not one that *vanishes*). See :mod:`advanced_omi_backend.services.job_reaper`.

It runs as a fire-and-forget asyncio task started in the app lifespan. All sweeps
share one coarse interval — this is the cold path, not the hot path.
"""

import asyncio
import logging
import time

from advanced_omi_backend.client_manager import get_client_manager
from advanced_omi_backend.config import WS_IDLE_TIMEOUT_SECS
from advanced_omi_backend.redis_factory import create_async_redis

logger = logging.getLogger(__name__)

# How often the reaper sweeps. Coarse — this is a backstop, not the hot path.
REAP_INTERVAL_SECS = 300

# A client is reaped once it has been silent for this long. Set well beyond the
# idle read-timeout so we only ever catch clients the primary path missed.
STALE_CLIENT_AGE_SECS = max(WS_IDLE_TIMEOUT_SECS * 3, 300)

# An orphaned per-client stream (no active client) gets this short TTL so consumer
# groups are released promptly without yanking data from an in-flight handoff.
ORPHAN_STREAM_TTL_SECS = 60


async def _reap_stale_clients() -> int:
    """Force-clean clients that have been silent past STALE_CLIENT_AGE_SECS."""
    # Lazy import to avoid a circular import at module load (websocket_controller
    # imports heavily from the rest of the backend).
    from advanced_omi_backend.controllers.websocket_controller import (
        cleanup_client_state,
    )

    manager = get_client_manager()
    now = time.time()
    reaped = 0
    for client_id, state in manager.get_all_clients().items():
        age = now - state.last_activity
        if age <= STALE_CLIENT_AGE_SECS:
            continue
        logger.warning(
            f"🧟 Reaping stale client {client_id} — silent for {age:.0f}s "
            f"(> {STALE_CLIENT_AGE_SECS:.0f}s); idle-timeout path did not fire"
        )
        try:
            await cleanup_client_state(client_id)
            reaped += 1
        except Exception as e:
            logger.error(f"Failed to reap stale client {client_id}: {e}", exc_info=True)
    return reaped


async def _reap_orphaned_streams() -> int:
    """Expire `audio:stream:{client_id}` keys that have no active client."""
    manager = get_client_manager()
    active = set(manager.get_all_client_ids())
    redis = create_async_redis(decode_responses=True)
    expired = 0
    try:
        async for key in redis.scan_iter(match="audio:stream:*", count=100):
            client_id = key.split("audio:stream:", 1)[1]
            if client_id in active:
                continue
            # Only set a TTL if the key is currently persistent (ttl == -1); leave
            # already-expiring keys alone so we don't reset a shorter countdown.
            if await redis.ttl(key) == -1:
                await redis.expire(key, ORPHAN_STREAM_TTL_SECS)
                expired += 1
                logger.info(
                    f"🧹 Reaper set {ORPHAN_STREAM_TTL_SECS}s TTL on orphaned stream {key}"
                )
    finally:
        await redis.close()
    return expired


async def _reap_orphaned_deferred_jobs() -> int:
    """Delete deferred RQ jobs whose dependency chain can never promote them."""
    # job_reaper uses synchronous RQ/Redis calls — run them off the event loop.
    from advanced_omi_backend.services.job_reaper import reap_orphaned_deferred_jobs

    result = await asyncio.to_thread(reap_orphaned_deferred_jobs)
    reaped = result.get("deleted", 0)
    for d in result.get("details", []):
        logger.warning(
            f"🧟 Reaped orphaned deferred job {d['job_id']} "
            f"(conv={d['conversation_id']}; {d['reason']})"
        )
    return reaped


async def run_reaper() -> None:
    """Periodic backstop sweep. Cancelled on app shutdown."""
    logger.info(
        f"🧹 Reaper started (every {REAP_INTERVAL_SECS}s; stale-client age "
        f"{STALE_CLIENT_AGE_SECS:.0f}s)"
    )
    while True:
        try:
            await asyncio.sleep(REAP_INTERVAL_SECS)
            clients = await _reap_stale_clients()
            streams = await _reap_orphaned_streams()
            jobs = await _reap_orphaned_deferred_jobs()
            if clients or streams or jobs:
                logger.info(
                    f"🧹 Reaper pass: reaped {clients} stale client(s), "
                    f"expired {streams} orphaned stream(s), "
                    f"reaped {jobs} orphaned deferred job(s)"
                )
        except asyncio.CancelledError:
            logger.info("🧹 Reaper stopped")
            raise
        except Exception as e:
            logger.error(f"Reaper pass failed: {e}", exc_info=True)
