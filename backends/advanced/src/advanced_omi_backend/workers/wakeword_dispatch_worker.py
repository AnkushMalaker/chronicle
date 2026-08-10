#!/usr/bin/env python3
"""
Wake-word dispatch worker.

Runs whenever an enabled plugin subscribes to ``wake_word.detected`` (see
``has_wakeword_dispatch_enabled`` in the worker registry). Consumes the standalone
wakeword-service's ``wakeword:detections`` Redis stream, batch-transcribes each
captured wake→turn-end audio window, and dispatches ``WAKE_WORD_DETECTED`` to the
plugin router.

This is intentionally decoupled from the live-transcription workers: the dispatcher
used to live inside the streaming-stt worker, so switching to windowed_batch (which
disables streaming-stt) silently broke the acoustic wake-word → plugin path. Running
it as its own worker keeps the acoustic path alive in every live-segmentation mode.
"""

import asyncio
import logging
import os
import signal
import sys

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.client_manager import initialize_redis_for_client_manager
from advanced_omi_backend.models.user import User
from advanced_omi_backend.redis_factory import REDIS_URL, create_async_redis
from advanced_omi_backend.services.observability.loop_monitor import start_loop_monitor
from advanced_omi_backend.services.plugin_service import (
    init_plugin_router,
    initialize_plugins,
    run_plugin_recovery,
)
from advanced_omi_backend.services.wakeword import WakeWordDispatcher

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://mongo:27017")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)

try:
    from advanced_omi_backend.services.observability.log_handler import (
        install_system_event_log_handler,
    )

    install_system_event_log_handler()
except Exception:  # noqa: BLE001 — never block worker startup on observability
    pass


async def main():
    """Main worker entry point."""
    logger.info("🚀 Starting wake-word dispatch worker")

    try:
        redis_client = create_async_redis(decode_responses=False)
        logger.info(f"✅ Connected to Redis: {REDIS_URL}")

        # ClientManager Redis for cross-container client→user mapping (used by plugins).
        initialize_redis_for_client_manager()
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}", exc_info=True)
        sys.exit(1)

    # MongoDB (Beanie) for the per-user wake-word speaker gate, which reads the
    # User document. This worker is otherwise Redis-only.
    try:
        mongo_client = AsyncIOMotorClient(MONGODB_URI)
        await init_beanie(database=mongo_client.chronicle, document_models=[User])
        logger.info("✅ Database (Beanie) initialized for speaker gate")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}", exc_info=True)
        sys.exit(1)

    # Initialize the plugin router so wake_word.detected reaches the plugins.
    try:
        plugin_router = init_plugin_router()
        if not plugin_router:
            logger.error(
                "No plugin router available — wake-word detections cannot be "
                "dispatched. Exiting."
            )
            await redis_client.aclose()
            sys.exit(1)

        logger.info(
            f"✅ Plugin router initialized with {len(plugin_router.plugins)} plugins"
        )
        await initialize_plugins(plugin_router)
    except Exception as e:
        logger.error(f"Failed to initialize plugin router: {e}", exc_info=True)
        await redis_client.aclose()
        sys.exit(1)

    # Background recovery for plugins whose external dependency was unreachable
    # at boot (e.g. Home Assistant on a server that's still off).
    recovery_task = asyncio.create_task(run_plugin_recovery(plugin_router))

    dispatcher = WakeWordDispatcher(
        redis_client=redis_client, plugin_router=plugin_router
    )

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        asyncio.create_task(dispatcher.stop())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("🔔 Listening for wake-word detections on wakeword:detections")
        start_loop_monitor("wakeword-dispatch")
        await dispatcher.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")
    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        recovery_task.cancel()
        await redis_client.aclose()
        logger.info("👋 Wake-word dispatch worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
