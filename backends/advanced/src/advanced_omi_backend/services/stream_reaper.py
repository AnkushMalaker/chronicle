"""Background reaper that backstops connection-lifecycle cleanup.

The WebSocket idle read-timeout (see `receive_with_idle_timeout` in
websocket_controller) is the primary mechanism that reaps a dead peer: when a
client stops sending, its handler raises WebSocketDisconnect and the `finally`
runs full cleanup. This reaper is the belt-and-suspenders backstop for the cases
that mechanism can't reach on its own:

1. A `ClientState` whose handler is wedged and never tripped the idle timeout —
   reaped by `last_activity` age so the Network page can never show an immortal
   "connected" zombie.
2. Per-client Redis audio streams left behind when cleanup never ran — expired so
   their consumer groups get released.

It runs as a fire-and-forget asyncio task started in the app lifespan.
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


async def run_stream_reaper() -> None:
    """Periodic backstop sweep. Cancelled on app shutdown."""
    logger.info(
        f"🧹 Stream reaper started (every {REAP_INTERVAL_SECS}s; stale-client age "
        f"{STALE_CLIENT_AGE_SECS:.0f}s)"
    )
    while True:
        try:
            await asyncio.sleep(REAP_INTERVAL_SECS)
            clients = await _reap_stale_clients()
            streams = await _reap_orphaned_streams()
            if clients or streams:
                logger.info(
                    f"🧹 Reaper pass: reaped {clients} stale client(s), "
                    f"expired {streams} orphaned stream(s)"
                )
        except asyncio.CancelledError:
            logger.info("🧹 Stream reaper stopped")
            raise
        except Exception as e:
            logger.error(f"Stream reaper pass failed: {e}", exc_info=True)
