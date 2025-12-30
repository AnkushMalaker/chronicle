#!/usr/bin/env python3
"""
Parakeet audio stream worker.

Starts a consumer that reads from audio:stream:* and transcribes audio using Parakeet.
"""

import asyncio
import logging
import os
import signal
import sys

import redis.asyncio as redis

from advanced_omi_backend.services.transcription.parakeet_stream_consumer import ParakeetStreamConsumer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)


async def main():
    """Main worker entry point."""
    logger.info("🚀 Starting Parakeet audio stream worker")

    # Check that config.yml has Parakeet configured
    # The registry provider will load configuration from config.yml
    service_url = os.getenv("PARAKEET_ASR_URL")
    if not service_url:
        logger.warning("PARAKEET_ASR_URL environment variable not set")
        logger.warning("Ensure config.yml has a default 'stt' model configured for Parakeet")
        logger.warning("Audio transcription will use alternative providers if configured in config.yml")

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Create Redis client
    redis_client = await redis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=False
    )
    logger.info("Connected to Redis")

    # Create consumer with balanced buffer size
    # 20 chunks = ~5 seconds of audio
    # Balance between transcription accuracy and latency
    # Consumer uses registry-driven provider from config.yml
    consumer = ParakeetStreamConsumer(
        redis_client=redis_client,
        buffer_chunks=20  # 5 seconds - good context without excessive delay
    )

    # Setup signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(signum, _frame):
        logger.info(f"Received signal {signum}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("✅ Parakeet worker ready")

        # This blocks until consumer is stopped or shutdown signaled
        consume_task = asyncio.create_task(consumer.start_consuming())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [consume_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()

        await consumer.stop()

    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await redis_client.aclose()
        logger.info("👋 Parakeet worker stopped")


if __name__ == "__main__":
    asyncio.run(main())

