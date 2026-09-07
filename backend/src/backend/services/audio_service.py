"""
Audio processing service using Redis Streams.

This service handles audio chunk streaming, processing, and coordination
using Redis Streams for event-driven architecture.
"""

import logging
import os
import time
from typing import Any, Dict, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")


class AudioStreamService:
    """
    Audio service using Redis Streams for event-driven processing.

    Architecture:
    - WebSocket publishes audio chunks to Redis Stream: audio:{client_id}
    - RQ workers consume from stream and process audio
    - Events published to transcript:events stream when transcription completes
    """

    def __init__(self, redis_url: Optional[str] = None):
        """Initialize audio stream service.

        Args:
            redis_url: Redis connection URL (defaults to REDIS_URL env var)
        """
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.redis: Optional[aioredis.Redis] = None

        # Stream configuration
        self.audio_stream_prefix = "audio:"  # audio:{client_id}
        self.transcript_events_stream = "transcript:events"
        self.memory_events_stream = "memory:events"

        # Consumer group names (action verbs - what they DO)
        self.memory_enqueuer = "memory-job-enqueuer"  # Enqueues memory extraction jobs
        self.event_listener = "event-listener"  # Listens for completion events

    async def connect(self):
        """Connect to Redis with connection pooling."""
        # Use connection pooling for better concurrency handling
        self.redis = await aioredis.from_url(
            self.redis_url,
            decode_responses=False,
            max_connections=20,  # Allow multiple concurrent operations
            socket_keepalive=True,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
        logger.info(f"Audio stream service connected to Redis at {self.redis_url}")

    async def disconnect(self):
        """Disconnect from Redis."""
        if self.redis:
            await self.redis.close()
            logger.info("Audio stream service disconnected from Redis")

    async def publish_transcript_event(
        self,
        audio_uuid: str,
        conversation_id: str,
        status: str,
        error: Optional[str] = None,
    ):
        """
        Publish transcript completion event.

        Args:
            audio_uuid: Audio UUID
            conversation_id: Conversation ID
            status: Status (completed, failed)
            error: Error message if failed
        """
        if not self.redis:
            raise RuntimeError("Redis not connected")

        event_data = {
            b"audio_uuid": audio_uuid.encode(),
            b"conversation_id": conversation_id.encode(),
            b"status": status.encode(),
            b"timestamp": str(int(time.time() * 1000)).encode(),
        }

        if error:
            event_data[b"error"] = error.encode()

        message_id = await self.redis.xadd(self.transcript_events_stream, event_data)

        logger.info(
            f"Published transcript event: {status} for {audio_uuid}, "
            f"message_id={message_id.decode()}"
        )

        # Ensure consumer group exists
        try:
            await self.redis.xgroup_create(
                self.transcript_events_stream,
                self.memory_enqueuer,
                id="0",
                mkstream=True,
            )
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def publish_memory_event(
        self,
        conversation_id: str,
        status: str,
        memory_count: int = 0,
        error: Optional[str] = None,
    ):
        """
        Publish memory processing event.

        Args:
            conversation_id: Conversation ID
            status: Status (completed, failed)
            memory_count: Number of memories extracted
            error: Error message if failed
        """
        if not self.redis:
            raise RuntimeError("Redis not connected")

        event_data = {
            b"conversation_id": conversation_id.encode(),
            b"status": status.encode(),
            b"memory_count": str(memory_count).encode(),
            b"timestamp": str(int(time.time() * 1000)).encode(),
        }

        if error:
            event_data[b"error"] = error.encode()

        message_id = await self.redis.xadd(self.memory_events_stream, event_data)

        logger.info(
            f"Published memory event: {status} for {conversation_id}, "
            f"memories={memory_count}, message_id={message_id.decode()}"
        )

        # Ensure consumer group exists
        try:
            await self.redis.xgroup_create(
                self.memory_events_stream, self.event_listener, id="0", mkstream=True
            )
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def get_stream_info(self, stream_name: str) -> Dict[str, Any]:
        """Get information about a stream."""
        if not self.redis:
            raise RuntimeError("Redis not connected")

        try:
            info = await self.redis.xinfo_stream(stream_name)
            return info
        except aioredis.ResponseError:
            return {}

    async def cleanup_old_messages(self, stream_name: str, max_age_ms: int = 3600000):
        """
        Trim old messages from stream (older than max_age_ms).

        Args:
            stream_name: Stream name
            max_age_ms: Maximum age in milliseconds (default 1 hour)
        """
        if not self.redis:
            raise RuntimeError("Redis not connected")
        if stream_name.startswith(self.audio_stream_prefix):
            raise RuntimeError(
                "Raw audio streams cannot be trimmed by age; deletion requires "
                "consumer-group durability proof"
            )

        # Calculate cutoff timestamp
        cutoff_ts = int((time.time() * 1000) - max_age_ms)

        # Trim stream
        await self.redis.xtrim(stream_name, minid=f"{cutoff_ts}-0", approximate=True)

        logger.debug(f"Trimmed old messages from {stream_name} (cutoff: {cutoff_ts})")


# Global singleton
_audio_stream_service: Optional[AudioStreamService] = None


def get_audio_stream_service() -> AudioStreamService:
    """Get the global audio stream service instance."""
    global _audio_stream_service
    if _audio_stream_service is None:
        _audio_stream_service = AudioStreamService()
    return _audio_stream_service
