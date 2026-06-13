#!/usr/bin/env python3
"""
Generic streaming transcription worker using registry-driven providers.

Starts a consumer that reads from audio:stream:* streams and transcribes via configured provider.
Provider configuration is loaded from config.yml (supports any streaming STT service).
Publishes interim results to Redis Pub/Sub for real-time client display.
Publishes final results to Redis Streams for storage.
Triggers plugins on final results only.
"""

import asyncio
import logging
import signal
import sys

from advanced_omi_backend.client_manager import initialize_redis_for_client_manager
from advanced_omi_backend.redis_factory import REDIS_URL, create_async_redis
from advanced_omi_backend.services.plugin_service import init_plugin_router
from advanced_omi_backend.services.transcription.streaming_consumer import (
    StreamingTranscriptionConsumer,
)
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)


async def main():
    """Main worker entry point."""
    logger.info("🚀 Starting streaming transcription worker")
    logger.info(
        "📋 Provider configuration loaded from config.yml (defaults.stt_stream)"
    )

    # Create Redis client
    try:
        redis_client = create_async_redis(decode_responses=False)
        logger.info(f"✅ Connected to Redis: {REDIS_URL}")

        # Initialize ClientManager Redis for cross-container client→user mapping
        initialize_redis_for_client_manager()

    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}", exc_info=True)
        sys.exit(1)

    # Initialize plugin router
    try:
        plugin_router = init_plugin_router()
        if plugin_router:
            logger.info(
                f"✅ Plugin router initialized with {len(plugin_router.plugins)} plugins"
            )

            # Initialize async plugins
            for plugin_id, plugin in plugin_router.plugins.items():
                try:
                    await plugin.initialize()
                    logger.info(
                        f"✅ Plugin '{plugin_id}' initialized in streaming worker"
                    )
                except Exception as e:
                    logger.exception(
                        f"Failed to initialize plugin '{plugin_id}' in streaming worker: {e}"
                    )
        else:
            logger.warning("No plugin router available - plugins will not be triggered")
    except Exception as e:
        logger.error(f"Failed to initialize plugin router: {e}", exc_info=True)
        plugin_router = None

    # Initialize speaker recognition client
    try:
        speaker_client = SpeakerRecognitionClient()
        if speaker_client.enabled:
            logger.info(
                f"Speaker recognition client initialized: {speaker_client.service_url}"
            )
        else:
            logger.info(
                "Speaker recognition disabled — streaming speaker identification off"
            )
            speaker_client = None
    except Exception as e:
        logger.warning(f"Failed to initialize speaker recognition client: {e}")
        speaker_client = None

    # Create streaming transcription consumer (uses registry-driven provider from config.yml)
    try:
        consumer = StreamingTranscriptionConsumer(
            redis_client=redis_client,
            plugin_router=plugin_router,
            speaker_client=speaker_client,
        )
        logger.info("Streaming transcription consumer created")
    except Exception as e:
        logger.error(
            f"Failed to create streaming transcription consumer: {e}", exc_info=True
        )
        logger.error(
            "Ensure config.yml has defaults.stt_stream configured with valid provider"
        )
        await redis_client.aclose()
        sys.exit(1)

    # The wake-word dispatcher runs as its own worker (wakeword_dispatch_worker),
    # decoupled from the live-transcription mode so the acoustic wake-word path keeps
    # working under windowed_batch too.

    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        asyncio.create_task(consumer.stop())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("✅ Streaming transcription worker ready")
        logger.info("📡 Listening for audio streams on audio:stream:* pattern")
        logger.info(
            "📢 Publishing interim results to transcription:interim:{session_id}"
        )
        logger.info("💾 Publishing final results to transcription:results:{session_id}")

        # The streaming consumer is the only task here; the wake-word dispatcher
        # runs as its own worker (wakeword_dispatch_worker).
        # heartbeat_name lets the workers healthcheck detect a wedged loop.
        await consumer.start_consuming(heartbeat_name="streaming-stt")

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")
    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await redis_client.aclose()
        logger.info("👋 Streaming transcription worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
