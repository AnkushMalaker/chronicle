"""
Audio stream producer - publishes audio chunks to Redis Streams.
"""

import logging
import time
from dataclasses import dataclass

import redis.asyncio as redis

from advanced_omi_backend.redis_factory import REDIS_URL, create_async_redis
from advanced_omi_backend.services.audio_stream.session_store import SessionStore

logger = logging.getLogger(__name__)


@dataclass
class SessionBuffer:
    """In-memory audio accumulator for one session.

    Holds incoming PCM until enough has arrived to emit a sample-aligned chunk
    to the session's Redis stream. Identity fields are duplicated here (they also
    live in the Redis session hash) so chunk/end messages can be built without a
    Redis round-trip on the hot path.
    """

    user_id: str
    client_id: str
    stream_name: str
    buffer: bytes = b""
    chunk_count: int = 0


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
        self.store = SessionStore(redis_client)

        # Per-session audio buffers for sample-aligned chunking: {session_id: SessionBuffer}
        self.session_buffers: dict[str, SessionBuffer] = {}

    async def init_session(
        self,
        session_id: str,
        user_id: str,
        client_id: str,
        user_email: str = "",
        connection_id: str = "",
        mode: str = "streaming",
        provider: str = "deepgram",
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

        # session_id is stable across reconnects (it equals client_id), so a new
        # WebSocket connection reuses the same Redis namespace as the previous one.
        # Clear connection-scoped keys that live OUTSIDE the session hash so the new
        # connection starts a clean transcription attempt:
        #
        #   transcription:complete  — set (5-min TTL) when the prior provider stream
        #     closed (graceful worker shutdown, stream-idle zombie exit, or end of a
        #     prior conversation). The streaming consumer's discovery loop SKIPS any
        #     stream whose completion flag exists, so a stale flag silently starves
        #     the reconnected session of streaming transcription — and the only code
        #     that clears it (open_conversation_job) never runs without results.
        #     Deleting it here breaks that deadlock.
        #
        #   transcription:results   — the prior connection's final-result stream.
        #     Left in place, the speech-detection aggregator reads PRE-reconnect text
        #     and can "detect speech" from stale content or interleave two timelines.
        try:
            await self.redis_client.delete(
                f"transcription:complete:{session_id}",
                f"transcription:results:{session_id}",
            )
        except Exception as e:  # noqa: BLE001 — never block session init on cleanup
            logger.warning(
                f"⚠️ Could not clear stale transcription keys for {session_id}: {e}"
            )

        # The session hash is the SINGLE SOURCE OF TRUTH for session state; the
        # SessionStore owns its schema. No TTL — sessions live until explicitly
        # cleaned up (TTLs destroy state mid-session, causing zombie jobs).
        await self.store.init_session(
            session_id,
            user_id=user_id,
            client_id=client_id,
            stream_name=stream_name,
            user_email=user_email,
            connection_id=connection_id,
            mode=mode,
            provider=provider,
        )

        # Initialize audio buffer for this session
        self.session_buffers[session_id] = SessionBuffer(
            user_id=user_id, client_id=client_id, stream_name=stream_name
        )

        logger.info(
            f"📊 Initialized session {session_id} → stream {stream_name} (provider: {provider})"
        )

    async def update_session_chunk_count(self, session_id: str):
        """
        Increment chunk counter and update last activity time.

        Args:
            session_id: Session identifier
        """
        await self.store.bump_chunk_count(session_id)

    async def send_session_end_signal(self, session_id: str):
        """
        Send end-of-session signal to workers to flush their buffers.

        Args:
            session_id: Session identifier
        """
        if session_id not in self.session_buffers:
            return

        buffer = self.session_buffers[session_id]
        stream_name = buffer.stream_name

        # Send special "end" message to signal workers to flush.
        # Read audio format from Redis session metadata (stored at audio-start time).
        sample_rate, channels, sample_width = await self.store.get_audio_format(
            session_id
        )

        end_signal = {
            b"audio_data": b"",  # Empty audio data
            b"session_id": session_id.encode(),
            b"chunk_id": b"END",  # Special marker
            b"user_id": buffer.user_id.encode(),
            b"client_id": buffer.client_id.encode(),
            b"timestamp": str(time.time()).encode(),
            b"sample_rate": str(sample_rate).encode(),
            b"channels": str(channels).encode(),
            b"sample_width": str(sample_width).encode(),
        }

        await self.redis_client.xadd(
            stream_name, end_signal, maxlen=25000, approximate=True
        )
        logger.info(f"📡 Sent end-of-session signal for {session_id} to {stream_name}")

    async def update_session_job_ids(
        self,
        session_id: str,
        speech_detection_job_id: str | None = None,
        audio_persistence_job_id: str | None = None,
    ):
        """
        Update job IDs in session metadata.

        Args:
            session_id: Session identifier
            speech_detection_job_id: Speech detection job ID (optional)
            audio_persistence_job_id: Audio persistence job ID (optional)
        """
        await self.store.set_job_ids(
            session_id,
            speech_detection_job_id=speech_detection_job_id,
            audio_persistence_job_id=audio_persistence_job_id,
        )

    async def finalize_session(
        self, session_id: str, completion_reason: str | None = None
    ):
        """
        Mark session as finalizing, send end marker, and clean up buffer.

        Args:
            session_id: Session identifier
            completion_reason: Optional reason for session completion (e.g., "websocket_disconnect", "user_stopped")
                              This is set atomically with status to avoid race conditions.
        """
        # Mark status=finalizing (+reason) atomically and notify the monitoring loop
        # via pub/sub. The completion_reason is set together with status to avoid the
        # race where a worker sees a finalizing status without its reason.
        if completion_reason:
            logger.info(
                f"📊 Finalizing session {session_id} with reason: {completion_reason}"
            )
        await self.store.mark_finalizing(session_id, completion_reason)

        # Send end_marker to Redis stream so streaming consumer can close the connection
        if session_id in self.session_buffers:
            buffer = self.session_buffers[session_id]
            stream_name = buffer.stream_name

            # Send end_marker message to signal stream end
            end_marker_data = {
                b"end_marker": b"true",
                b"session_id": session_id.encode(),
                b"user_id": buffer.user_id.encode(),
                b"client_id": buffer.client_id.encode(),
                b"timestamp": str(time.time()).encode(),
            }

            await self.redis_client.xadd(
                stream_name, end_marker_data, maxlen=25000, approximate=True
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
        user_id: str,
        client_id: str,
        sample_rate: int = 16000,
        channels: int = 1,
        sample_width: int = 2,
    ) -> list[str]:
        """
        Add audio data to session buffer and publish fixed-size chunks.

        Buffers incoming audio and creates sample-aligned chunks of fixed duration
        (0.25 seconds = 8000 bytes for 16kHz 16-bit mono) to prevent cutting mid-word.

        Args:
            audio_data: Raw PCM audio bytes (arbitrary size from WebSocket)
            session_id: Session identifier
            user_id: User identifier
            client_id: Client identifier (used for stream naming)
            sample_rate: Audio sample rate (Hz)
            channels: Number of audio channels
            sample_width: Bytes per sample

        Returns:
            List of Redis message IDs (may send multiple chunks per call)
        """
        # Initialize buffer if needed. This is a fallback — init_session() normally
        # creates it first; reaching here means audio arrived before session init.
        if session_id not in self.session_buffers:
            logger.warning(
                f"⚠️ add_audio_chunk before init_session for {session_id}; "
                f"creating buffer on the fly"
            )
            self.session_buffers[session_id] = SessionBuffer(
                user_id=user_id,
                client_id=client_id,
                stream_name=f"audio:stream:{client_id}",  # Client-specific stream
            )

        session_buffer = self.session_buffers[session_id]

        # Add incoming audio to buffer
        session_buffer.buffer += audio_data

        # Calculate target chunk size (0.25 seconds of audio)
        # bytes_per_second = sample_rate * channels * sample_width
        # target_chunk_duration = 0.25 seconds
        bytes_per_second = sample_rate * channels * sample_width
        target_chunk_size = int(bytes_per_second * 0.25)

        # Publish fixed-size chunks from buffer
        message_ids = []
        stream_name = session_buffer.stream_name

        while len(session_buffer.buffer) >= target_chunk_size:
            # Extract exactly target_chunk_size bytes
            chunk_audio = session_buffer.buffer[:target_chunk_size]
            session_buffer.buffer = session_buffer.buffer[target_chunk_size:]

            # Increment chunk count
            session_buffer.chunk_count += 1
            chunk_id_formatted = f"{session_buffer.chunk_count:05d}"

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
                approximate=True,
            )
            message_ids.append(message_id.decode())

            # Update session tracking
            await self.update_session_chunk_count(session_id)

            # Log every 10th chunk to avoid spam
            if session_buffer.chunk_count % 10 == 0 or session_buffer.chunk_count <= 5:
                logger.debug(
                    f"📤 Added fixed-size chunk {chunk_id_formatted} to {stream_name} "
                    f"({len(chunk_audio)} bytes = {len(chunk_audio)/bytes_per_second:.3f}s, "
                    f"buffer remaining: {len(session_buffer.buffer)} bytes)"
                )

        # Log buffer accumulation if no chunks were sent
        if not message_ids:
            logger.debug(
                f"📦 Buffering audio for {session_id}: "
                f"{len(session_buffer.buffer)}/{target_chunk_size} bytes "
                f"(need {target_chunk_size - len(session_buffer.buffer)} more)"
            )

        return message_ids

    async def flush_session_buffer(
        self,
        session_id: str,
        sample_rate: int = 16000,
        channels: int = 1,
        sample_width: int = 2,
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
        if len(session_buffer.buffer) > 0:
            chunk_audio = session_buffer.buffer
            session_buffer.buffer = b""

            # Increment chunk count
            session_buffer.chunk_count += 1
            chunk_id_formatted = f"{session_buffer.chunk_count:05d}"

            stream_name = session_buffer.stream_name

            # Prepare chunk data
            chunk_data = {
                b"audio_data": chunk_audio,
                b"session_id": session_id.encode(),
                b"chunk_id": chunk_id_formatted.encode(),
                b"user_id": session_buffer.user_id.encode(),
                b"client_id": session_buffer.client_id.encode(),
                b"timestamp": str(time.time()).encode(),
                b"sample_rate": str(sample_rate).encode(),
                b"channels": str(channels).encode(),
                b"sample_width": str(sample_width).encode(),
            }

            # Add to stream with MAXLEN limit
            message_id = await self.redis_client.xadd(
                stream_name, chunk_data, maxlen=25000, approximate=True
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

        # Create async Redis client (synchronous call, connection happens on first use)
        redis_client = create_async_redis(decode_responses=False)

        _producer_instance = AudioStreamProducer(redis_client)
        logger.info(
            f"Created AudioStreamProducer singleton with Redis URL: {REDIS_URL}"
        )

    return _producer_instance
