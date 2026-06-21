#!/usr/bin/env python3
"""
Windowed batch transcription worker.

Runs when defaults.live_segmentation == "windowed_batch" (set when no streaming ASR is
configured). Consumes audio:stream:*, buffers fixed-duration windows, batch-transcribes
each window with the configured batch STT provider, and writes results to
transcription:results:{session_id} — so long/continuous sources are transcribed
incrementally instead of only on disconnect.
"""

import asyncio
import logging
import signal
import sys

from advanced_omi_backend.config_loader import get_backend_config
from advanced_omi_backend.redis_factory import REDIS_URL, create_async_redis
from advanced_omi_backend.services.audio_stream.windowed_batch_consumer import (
    WindowedBatchConsumer,
)

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
    logger.info("🚀 Starting windowed batch transcription worker")

    try:
        redis_client = create_async_redis(decode_responses=False)
        logger.info(f"✅ Connected to Redis: {REDIS_URL}")
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}", exc_info=True)
        sys.exit(1)

    # Window size from config.yml (backend.transcription.windowed_batch_seconds)
    try:
        transcription_cfg = get_backend_config("transcription")
        window_seconds = float(transcription_cfg.get("windowed_batch_seconds", 30.0))
    except Exception as e:
        logger.warning(f"Failed to read windowed_batch_seconds, using 30s: {e}")
        window_seconds = 30.0

    try:
        consumer = WindowedBatchConsumer(
            redis_client=redis_client, window_seconds=window_seconds
        )
    except Exception as e:
        logger.error(f"Failed to create windowed batch consumer: {e}", exc_info=True)
        logger.error("Ensure config.yml has defaults.stt (batch provider) configured")
        await redis_client.aclose()
        sys.exit(1)

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        asyncio.create_task(consumer.stop())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info(f"✅ Windowed batch worker ready (window={window_seconds:.0f}s)")
        logger.info("📡 Listening for audio streams on audio:stream:* pattern")
        logger.info("💾 Publishing results to transcription:results:{session_id}")

        # This blocks until the consumer is stopped.
        # heartbeat_name lets the workers healthcheck detect a wedged loop.
        await consumer.start_consuming(heartbeat_name="windowed-batch")

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")
    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await redis_client.aclose()
        logger.info("👋 Windowed batch worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
