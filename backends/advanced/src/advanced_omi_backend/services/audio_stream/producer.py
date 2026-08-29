"""
Audio stream producer - publishes audio chunks to Redis Streams.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import redis.asyncio as redis
from redis.exceptions import WatchError

from advanced_omi_backend.models.audio_capture import (
    CAPTURE_CONTINUITY_TOLERANCE_SECONDS,
    AudioCaptureSession,
    CaptureEffects,
    CaptureProcessingProfile,
)
from advanced_omi_backend.redis_factory import REDIS_URL, create_async_redis
from advanced_omi_backend.redis_keys import audio_session
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
    captured_at: float | None = None
    time_basis: str = "received"


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

    async def _append_capture_message(
        self, session_id: str, stream_name: str, fields: dict[bytes, bytes]
    ):
        """Append one WAL entry while atomically verifying capture is active.

        The entry is identified by the technical capture session. No Conversation
        needs to exist, and semantic segmentation cannot interrupt this write path.
        """
        session_key = audio_session(session_id)
        while True:
            async with self.redis_client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(session_key)
                    raw_status = await pipe.hget(session_key, "status")
                    status = (
                        raw_status.decode()
                        if isinstance(raw_status, bytes)
                        else raw_status
                    )
                    if status != "active":
                        await pipe.unwatch()
                        raise RuntimeError(
                            f"Audio session {session_id} is not active ({status})"
                        )
                    capture_fields = dict(fields)
                    capture_fields[b"capture_session_id"] = session_id.encode()

                    pipe.multi()
                    pipe.xadd(stream_name, capture_fields)
                    result = await pipe.execute()
                    return result[0]
                except WatchError:
                    continue

    async def init_session(
        self,
        session_id: str,
        user_id: str,
        client_id: str,
        *,
        user_email: str = "",
        connection_id: str = "",
        mode: str = "streaming",
        provider: str = "deepgram",
        capture_epoch: int,
        processing_profile: CaptureProcessingProfile,
        effects: CaptureEffects,
        voice_session_id: str | None,
        data_purpose: str = "normal_capture",
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
        # One immutable write-ahead log per recording attempt. A reconnect gets a
        # new session/stream, so it cannot reset or append behind an older worker
        # that is still draining.
        stream_name = f"audio:stream:{session_id}"

        # Raw audio streams are write-ahead logs.  A prior version put a short TTL
        # on disconnect; remove any inherited TTL before this connection can append
        # new bytes.  Retention is now owned exclusively by the durability gate
        # after every persistence consumer has drained.
        await self.redis_client.persist(stream_name)

        # Clear connection-scoped keys that live OUTSIDE the session hash so the
        # new connection starts a clean transcription attempt:
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
            capture_epoch=capture_epoch,
            processing_profile=processing_profile,
            effects=effects.model_dump(mode="json"),
            voice_session_id=voice_session_id,
        )

        capture = AudioCaptureSession(
            capture_session_id=session_id,
            user_id=user_id,
            capture_source_id=client_id,
            client_id=client_id,
            origin="streaming" if mode == "streaming" else "batch",
            time_basis=(
                "captured"
                if processing_profile
                in {"duplex_aec", "duplex_isolated", "half_duplex"}
                else "received"
            ),
            capture_epoch=capture_epoch,
            processing_profile=processing_profile,
            effects=effects,
            voice_session_id=voice_session_id,
            source_stream=stream_name,
            data_purpose=data_purpose,
        )
        await capture.insert()

        if session_id in self.session_buffers:
            raise RuntimeError(f"Audio session {session_id} is already initialized")

        # Initialize audio buffer for this session
        self.session_buffers[session_id] = SessionBuffer(
            user_id=user_id,
            client_id=client_id,
            stream_name=stream_name,
            time_basis=(
                "captured"
                if processing_profile
                in {"duplex_aec", "duplex_isolated", "half_duplex"}
                else "received"
            ),
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
            raise RuntimeError(f"Audio session {session_id} has no producer buffer")

        buffer = self.session_buffers[session_id]
        stream_name = buffer.stream_name

        # Send special "end" message to signal workers to flush.
        # Read audio format from Redis session metadata (stored at audio-start time).
        sample_rate, channels, sample_width = await self.store.get_audio_format(
            session_id
        )

        end_signal = {
            b"audio_data": b"",  # Empty audio data
            b"end_marker": b"true",
            b"session_id": session_id.encode(),
            b"chunk_id": b"END",  # Special marker
            b"user_id": buffer.user_id.encode(),
            b"client_id": buffer.client_id.encode(),
            b"timestamp": str(time.time()).encode(),
            b"sample_rate": str(sample_rate).encode(),
            b"channels": str(channels).encode(),
            b"sample_width": str(sample_width).encode(),
        }

        await self._append_capture_message(session_id, stream_name, end_signal)
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
        # Finalization is deliberately ordered:
        #
        #   buffered process memory -> Redis audio log -> terminal marker -> status
        #
        # A failure leaves the session in its prior ACTIVE state with the bytes still
        # either in this buffer or in Redis.  We never publish FINALIZING first and
        # then discover that the last bytes could not be durably accepted.
        if completion_reason:
            logger.info(
                f"📊 Finalizing session {session_id} with reason: {completion_reason}"
            )
        if session_id in self.session_buffers:
            sample_rate, channels, sample_width = await self.store.get_audio_format(
                session_id
            )
            await self.flush_session_buffer(
                session_id,
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
            )
            await self.send_session_end_signal(session_id)

        # status + completion reason are one SessionStore write.  At this point all
        # producer-owned bytes and the marker are already in the Redis WAL.
        await self.store.mark_finalizing(session_id, completion_reason)

        capture = await AudioCaptureSession.find_one(
            AudioCaptureSession.capture_session_id == session_id
        )
        if capture is None:
            raise RuntimeError(f"Capture session {session_id} disappeared")
        capture.status = "finalizing"
        capture.ended_at = datetime.now(timezone.utc)
        await capture.save()

        if session_id in self.session_buffers:
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
        captured_at: float | None = None,
        time_basis: str | None = None,
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
        if session_id not in self.session_buffers:
            raise RuntimeError(
                f"Audio reached uninitialized session {session_id}; refusing ingress"
            )

        session_buffer = self.session_buffers[session_id]
        if session_buffer.user_id != user_id or session_buffer.client_id != client_id:
            raise RuntimeError(
                f"Audio identity mismatch for initialized session {session_id}"
            )

        bytes_per_second = sample_rate * channels * sample_width
        target_chunk_size = int(bytes_per_second * 0.25)
        incoming_time_basis = time_basis or (
            "recorded" if captured_at is not None else "received"
        )
        if incoming_time_basis not in {"captured", "recorded", "received"}:
            raise ValueError(
                f"Invalid incoming capture time basis: {incoming_time_basis}"
            )
        incoming_captured_at = captured_at if captured_at is not None else time.time()

        # A partial producer chunk is still physical audio. Close it before a clock
        # seam so the first resumed sample cannot be packed behind pre-gap samples.
        if session_buffer.buffer:
            expected_at = (session_buffer.captured_at or incoming_captured_at) + (
                len(session_buffer.buffer) / bytes_per_second
            )
            discontinuity_seconds = incoming_captured_at - expected_at
            if (
                session_buffer.time_basis != incoming_time_basis
                or abs(discontinuity_seconds) > CAPTURE_CONTINUITY_TOLERANCE_SECONDS
            ):
                logger.info(
                    "Flushing producer buffer for capture %s at %+.3fs "
                    "timestamp discontinuity",
                    session_id,
                    discontinuity_seconds,
                )
                await self.flush_session_buffer(
                    session_id,
                    sample_rate=sample_rate,
                    channels=channels,
                    sample_width=sample_width,
                )

        # Add incoming audio to buffer
        if not session_buffer.buffer:
            session_buffer.captured_at = incoming_captured_at
            session_buffer.time_basis = incoming_time_basis
        session_buffer.buffer += audio_data

        # Calculate target chunk size (0.25 seconds of audio)
        # bytes_per_second = sample_rate * channels * sample_width
        # target_chunk_duration = 0.25 seconds
        # Publish fixed-size chunks from buffer
        message_ids = []
        stream_name = session_buffer.stream_name

        while len(session_buffer.buffer) >= target_chunk_size:
            # Build the next append without mutating process memory.  XADD is the
            # durability boundary: only after Redis confirms it do we advance the
            # buffer and sequence number.
            chunk_audio = session_buffer.buffer[:target_chunk_size]
            next_chunk_count = session_buffer.chunk_count + 1
            chunk_id_formatted = f"{next_chunk_count:05d}"

            # Prepare chunk data
            chunk_data = {
                b"audio_data": chunk_audio,
                b"session_id": session_id.encode(),
                b"chunk_id": chunk_id_formatted.encode(),
                b"user_id": user_id.encode(),
                b"client_id": client_id.encode(),
                b"timestamp": str(time.time()).encode(),
                b"captured_at": str(session_buffer.captured_at or time.time()).encode(),
                b"time_basis": session_buffer.time_basis.encode(),
                b"sample_rate": str(sample_rate).encode(),
                b"channels": str(channels).encode(),
                b"sample_width": str(sample_width).encode(),
            }

            # Never cap the shared raw-audio WAL. A consumer-independent MAXLEN can
            # trim unread/pending data out from under Mongo persistence.
            message_id = await self._append_capture_message(
                session_id, stream_name, chunk_data
            )

            session_buffer.buffer = session_buffer.buffer[target_chunk_size:]
            session_buffer.captured_at = (
                (session_buffer.captured_at or time.time())
                + (len(chunk_audio) / bytes_per_second)
                if session_buffer.buffer
                else None
            )
            session_buffer.chunk_count = next_chunk_count
            message_ids.append(
                message_id.decode() if isinstance(message_id, bytes) else message_id
            )

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
            next_chunk_count = session_buffer.chunk_count + 1
            chunk_id_formatted = f"{next_chunk_count:05d}"

            stream_name = session_buffer.stream_name

            # Prepare chunk data
            chunk_data = {
                b"audio_data": chunk_audio,
                b"session_id": session_id.encode(),
                b"chunk_id": chunk_id_formatted.encode(),
                b"user_id": session_buffer.user_id.encode(),
                b"client_id": session_buffer.client_id.encode(),
                b"timestamp": str(time.time()).encode(),
                b"captured_at": str(session_buffer.captured_at or time.time()).encode(),
                b"time_basis": session_buffer.time_basis.encode(),
                b"sample_rate": str(sample_rate).encode(),
                b"channels": str(channels).encode(),
                b"sample_width": str(sample_width).encode(),
            }

            # Keep the process buffer intact if Redis rejects the append.
            message_id = await self._append_capture_message(
                session_id, stream_name, chunk_data
            )
            session_buffer.buffer = b""
            session_buffer.captured_at = None
            session_buffer.chunk_count = next_chunk_count

            # Update session tracking
            await self.update_session_chunk_count(session_id)

            bytes_per_second = sample_rate * channels * sample_width
            logger.info(
                f"📤 Flushed final chunk {chunk_id_formatted} to {stream_name} "
                f"({len(chunk_audio)} bytes = {len(chunk_audio)/bytes_per_second:.3f}s)"
            )

            return message_id.decode() if isinstance(message_id, bytes) else message_id

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
