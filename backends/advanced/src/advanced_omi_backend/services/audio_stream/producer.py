"""
Audio stream producer - publishes audio chunks to Redis Streams.
"""

import logging
import time

import redis.asyncio as redis

from advanced_omi_backend.services.transcription.base import TranscriptionProvider

logger = logging.getLogger(__name__)


class AudioStreamProducer:
    """
    Publishes audio chunks to provider-specific Redis Streams.

    Routes audio to: audio:stream:{provider} (e.g., "audio:stream:deepgram")

    Multiple workers can consume from the same stream using consumer groups for horizontal scaling.
    Buffers incoming audio and creates fixed-size chunks aligned to sample boundaries.
    This prevents cutting audio mid-word and improves transcription accuracy.
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize producer.

        Args:
            redis_client: Connected Redis client
        """
        self.redis_client = redis_client

        # Per-session audio buffers for sample-aligned chunking
        # {session_id: {"buffer": bytes, "chunk_count": int, "stream_name": str, ...}}
        self.session_buffers = {}

    async def init_session(
        self,
        session_id: str,
        user_id: str,
        client_id: str,
        user_email: str = "",
        connection_id: str = "",
        mode: str = "streaming",
        provider: str = "deepgram"
    ):
        """
        Initialize session tracking metadata in Redis.

        This is the SINGLE SOURCE OF TRUTH for session state.
        All session metadata is stored here instead of in-memory ClientState.

        Args:
            session_id: Unique session identifier
            user_id: User identifier (MongoDB ObjectId)
            client_id: Client identifier (objectid_suffix-device_name)
            user_email: User email for debugging/tracking
            connection_id: WebSocket connection identifier
            mode: Processing mode (streaming/batch)
            provider: Transcription provider from config.yml
        """
        # Client-specific stream naming (one stream per client for isolation)
        stream_name = f"audio:stream:{client_id}"
        session_key = f"audio:session:{session_id}"

        await self.redis_client.hset(session_key, mapping={
            # User & Client tracking
            "user_id": user_id,
            "user_email": user_email,
            "client_id": client_id,
            "connection_id": connection_id,

            # Stream configuration
            "stream_name": stream_name,
            "provider": provider,
            "mode": mode,

            # Timestamps
            "started_at": str(time.time()),
            "last_chunk_at": str(time.time()),

            # Counters
            "chunks_published": "0",

            # Job tracking (populated by queue_controller when jobs start)
            "speech_detection_job_id": "",
            "audio_persistence_job_id": "",

            # Connection state
            "websocket_connected": "true",

            # Session status
            "status": "active"
        })

        # Set TTL of 1 hour
        await self.redis_client.expire(session_key, 3600)

        # Initialize audio buffer for this session
        self.session_buffers[session_id] = {
            "buffer": b"",
            "chunk_count": 0,
            "user_id": user_id,
            "client_id": client_id,
            "stream_name": stream_name,
            "provider": provider
        }

        logger.info(f"📊 Initialized session {session_id} → stream {stream_name} (provider: {provider})")

    async def update_session_chunk_count(self, session_id: str):
        """
        Increment chunk counter and update last activity time.

        Args:
            session_id: Session identifier
        """
        session_key = f"audio:session:{session_id}"

        # Increment chunk count
        await self.redis_client.hincrby(session_key, "chunks_published", 1)

        # Update last chunk time
        await self.redis_client.hset(session_key, "last_chunk_at", str(time.time()))

    async def send_session_end_signal(self, session_id: str):
        """
        Send end-of-session signal to workers to flush their buffers.

        Args:
            session_id: Session identifier
        """
        if session_id not in self.session_buffers:
            return

        buffer = self.session_buffers[session_id]
        stream_name = buffer["stream_name"]

        # Send special "end" message to signal workers to flush
        end_signal = {
            b"audio_data": b"",  # Empty audio data
            b"session_id": session_id.encode(),
            b"chunk_id": b"END",  # Special marker
            b"user_id": buffer["user_id"].encode(),
            b"client_id": buffer["client_id"].encode(),
            b"timestamp": str(time.time()).encode(),
            b"sample_rate": b"16000",
            b"channels": b"1",
            b"sample_width": b"2",
        }

        await self.redis_client.xadd(
            stream_name,
            end_signal,
            maxlen=25000,
            approximate=True
        )
        logger.info(f"📡 Sent end-of-session signal for {session_id} to {stream_name}")

    async def get_session(self, session_id: str) -> dict:
        """
        Get session metadata from Redis.

        Args:
            session_id: Session identifier

        Returns:
            Dictionary with session metadata, empty dict if not found
        """
        session_key = f"audio:session:{session_id}"
        session_data = await self.redis_client.hgetall(session_key)

        # Convert bytes to strings for easier handling
        return {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in session_data.items()} if session_data else {}

    async def update_session_job_ids(
        self,
        session_id: str,
        speech_detection_job_id: str = None,
        audio_persistence_job_id: str = None
    ):
        """
        Update job IDs in session metadata.

        Args:
            session_id: Session identifier
            speech_detection_job_id: Speech detection job ID (optional)
            audio_persistence_job_id: Audio persistence job ID (optional)
        """
        session_key = f"audio:session:{session_id}"
        updates = {}

        if speech_detection_job_id:
            updates["speech_detection_job_id"] = speech_detection_job_id
        if audio_persistence_job_id:
            updates["audio_persistence_job_id"] = audio_persistence_job_id

        if updates:
            await self.redis_client.hset(session_key, mapping=updates)
            logger.debug(f"📊 Updated job IDs for session {session_id}: {updates}")

    async def mark_websocket_disconnected(self, session_id: str):
        """
        Mark session's websocket as disconnected.

        Args:
            session_id: Session identifier
        """
        session_key = f"audio:session:{session_id}"
        await self.redis_client.hset(session_key, mapping={
            "websocket_connected": "false",
            "disconnected_at": str(time.time())
        })
        logger.info(f"🔌 Marked websocket disconnected for session {session_id}")

    async def finalize_session(self, session_id: str, completion_reason: str = None):
        """
        Mark session as finalizing, send end marker, and clean up buffer.

        Args:
            session_id: Session identifier
            completion_reason: Optional reason for session completion (e.g., "websocket_disconnect", "user_stopped")
                              This is set atomically with status to avoid race conditions.
        """
        session_key = f"audio:session:{session_id}"

        # Build mapping with status and optional completion_reason
        mapping = {
            "status": "finalizing",
            "finalized_at": str(time.time())
        }

        # Set completion_reason atomically with status to prevent race conditions
        if completion_reason:
            mapping["completion_reason"] = completion_reason
            logger.info(f"📊 Finalizing session {session_id} with reason: {completion_reason}")

        await self.redis_client.hset(session_key, mapping=mapping)

        # Send end_marker to Redis stream so streaming consumer can close the connection
        if session_id in self.session_buffers:
            buffer = self.session_buffers[session_id]
            stream_name = buffer["stream_name"]

            # Send end_marker message to signal stream end
            end_marker_data = {
                b"end_marker": b"true",
                b"session_id": session_id.encode(),
                b"user_id": buffer["user_id"].encode(),
                b"client_id": buffer["client_id"].encode(),
                b"timestamp": str(time.time()).encode(),
            }

            await self.redis_client.xadd(
                stream_name,
                end_marker_data,
                maxlen=25000,
                approximate=True
            )
            logger.info(f"📡 Sent end_marker to {stream_name} for session {session_id}")

            # Clean up session buffer
            del self.session_buffers[session_id]
            logger.debug(f"🧹 Cleaned up buffer for session {session_id}")

        logger.info(f"📊 Marked session {session_id} as finalizing")

    async def add_audio_chunk(
        self,
        audio_data: bytes,
        session_id: str,
        chunk_id: str,
        user_id: str,
        client_id: str,
        sample_rate: int = 16000,
        channels: int = 1,
        sample_width: int = 2
    ) -> list[str]:
        """
        Add audio data to session buffer and publish fixed-size chunks.

        Buffers incoming audio and creates sample-aligned chunks of fixed duration
        (0.25 seconds = 8000 bytes for 16kHz 16-bit mono) to prevent cutting mid-word.

        Args:
            audio_data: Raw PCM audio bytes (arbitrary size from WebSocket)
            session_id: Session identifier
            chunk_id: Base chunk identifier (will increment for multiple chunks)
            user_id: User identifier
            client_id: Client identifier (used for stream naming)
            sample_rate: Audio sample rate (Hz)
            channels: Number of audio channels
            sample_width: Bytes per sample

        Returns:
            List of Redis message IDs (may send multiple chunks per call)
        """
        # Initialize buffer if needed (in case init_session wasn't called)
        if session_id not in self.session_buffers:
            stream_name = f"audio:stream:{client_id}"  # Client-specific stream
            self.session_buffers[session_id] = {
                "buffer": b"",
                "chunk_count": 0,
                "user_id": user_id,
                "client_id": client_id,
                "stream_name": stream_name,
                "provider": "deepgram"
            }

        session_buffer = self.session_buffers[session_id]

        # Add incoming audio to buffer
        session_buffer["buffer"] += audio_data

        # Calculate target chunk size (0.25 seconds of audio)
        # bytes_per_second = sample_rate * channels * sample_width
        # target_chunk_duration = 0.25 seconds
        bytes_per_second = sample_rate * channels * sample_width
        target_chunk_size = int(bytes_per_second * 0.25)

        # Publish fixed-size chunks from buffer
        message_ids = []
        stream_name = session_buffer["stream_name"]

        while len(session_buffer["buffer"]) >= target_chunk_size:
            # Extract exactly target_chunk_size bytes
            chunk_audio = session_buffer["buffer"][:target_chunk_size]
            session_buffer["buffer"] = session_buffer["buffer"][target_chunk_size:]

            # Increment chunk count
            session_buffer["chunk_count"] += 1
            chunk_id_formatted = f"{session_buffer['chunk_count']:05d}"

            # Prepare chunk data
            chunk_data = {
                b"audio_data": chunk_audio,
                b"session_id": session_id.encode(),
                b"chunk_id": chunk_id_formatted.encode(),
                b"user_id": user_id.encode(),
                b"client_id": client_id.encode(),
                b"timestamp": str(time.time()).encode(),
                b"sample_rate": str(sample_rate).encode(),
                b"channels": str(channels).encode(),
                b"sample_width": str(sample_width).encode(),
            }

            # Add to stream with MAXLEN limit (safety net to prevent unbounded growth)
            message_id = await self.redis_client.xadd(
                stream_name,
                chunk_data,
                maxlen=25000,  # Keep max 25k chunks (~104 minutes at 250ms/chunk)
                approximate=True
            )
            message_ids.append(message_id.decode())

            # Update session tracking
            await self.update_session_chunk_count(session_id)

            # Log every 10th chunk to avoid spam
            if session_buffer["chunk_count"] % 10 == 0 or session_buffer["chunk_count"] <= 5:
                logger.debug(
                    f"📤 Added fixed-size chunk {chunk_id_formatted} to {stream_name} "
                    f"({len(chunk_audio)} bytes = {len(chunk_audio)/bytes_per_second:.3f}s, "
                    f"buffer remaining: {len(session_buffer['buffer'])} bytes)"
                )

        # Log buffer accumulation if no chunks were sent
        if not message_ids:
            logger.debug(
                f"📦 Buffering audio for {session_id}: "
                f"{len(session_buffer['buffer'])}/{target_chunk_size} bytes "
                f"(need {target_chunk_size - len(session_buffer['buffer'])} more)"
            )

        return message_ids

    async def flush_session_buffer(
        self,
        session_id: str,
        sample_rate: int = 16000,
        channels: int = 1,
        sample_width: int = 2
    ) -> str | None:
        """
        Flush any remaining audio in session buffer.

        Called at session end to send the last partial chunk.

        Args:
            session_id: Session identifier
            sample_rate: Audio sample rate (Hz)
            channels: Number of audio channels
            sample_width: Bytes per sample

        Returns:
            Redis message ID if chunk was sent, None if buffer was empty
        """
        if session_id not in self.session_buffers:
            return None

        session_buffer = self.session_buffers[session_id]

        # Send any remaining buffered audio
        if len(session_buffer["buffer"]) > 0:
            chunk_audio = session_buffer["buffer"]
            session_buffer["buffer"] = b""

            # Increment chunk count
            session_buffer["chunk_count"] += 1
            chunk_id_formatted = f"{session_buffer['chunk_count']:05d}"

            stream_name = session_buffer["stream_name"]

            # Prepare chunk data
            chunk_data = {
                b"audio_data": chunk_audio,
                b"session_id": session_id.encode(),
                b"chunk_id": chunk_id_formatted.encode(),
                b"user_id": session_buffer["user_id"].encode(),
                b"client_id": session_buffer["client_id"].encode(),
                b"timestamp": str(time.time()).encode(),
                b"sample_rate": str(sample_rate).encode(),
                b"channels": str(channels).encode(),
                b"sample_width": str(sample_width).encode(),
            }

            # Add to stream with MAXLEN limit
            message_id = await self.redis_client.xadd(
                stream_name,
                chunk_data,
                maxlen=25000,
                approximate=True
            )

            # Update session tracking
            await self.update_session_chunk_count(session_id)

            bytes_per_second = sample_rate * channels * sample_width
            logger.info(
                f"📤 Flushed final chunk {chunk_id_formatted} to {stream_name} "
                f"({len(chunk_audio)} bytes = {len(chunk_audio)/bytes_per_second:.3f}s)"
            )

            return message_id.decode()

        return None



# Singleton instance
_producer_instance = None


def get_audio_stream_producer() -> AudioStreamProducer:
    """
    Get or create singleton AudioStreamProducer instance.

    Returns:
        Singleton AudioStreamProducer instance
    """
    global _producer_instance

    if _producer_instance is None:
        import os

        import redis.asyncio as redis_async

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

        # Create async Redis client (synchronous call, connection happens on first use)
        redis_client = redis_async.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=False
        )

        _producer_instance = AudioStreamProducer(redis_client)
        logger.info(f"Created AudioStreamProducer singleton with Redis URL: {redis_url}")

    return _producer_instance
