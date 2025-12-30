#!/usr/bin/env python3
"""
Deepgram audio stream worker.

Starts a consumer that reads from audio:stream:deepgram and transcribes audio.
"""

import asyncio
import logging
import os
import signal
import sys

import redis.asyncio as redis

from advanced_omi_backend.services.transcription.deepgram import DeepgramStreamConsumer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)


async def main():
    """Main worker entry point."""
    logger.info("🚀 Starting Deepgram audio stream worker")

    # Check that config.yml has Deepgram configured
    # The registry provider will load configuration from config.yml
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        logger.warning("DEEPGRAM_API_KEY environment variable not set")
        logger.warning("Ensure config.yml has a default 'stt' model configured for Deepgram")
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
    consumer = DeepgramStreamConsumer(
        redis_client=redis_client,
        buffer_chunks=20  # 5 seconds - good context without excessive delay
    )

    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        asyncio.create_task(consumer.stop())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("✅ Deepgram worker ready")

        # This blocks until consumer is stopped
        await consumer.start_consuming()

    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await redis_client.aclose()
        logger.info("👋 Deepgram worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
