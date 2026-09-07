"""
Base audio stream consumer - reads from Redis Streams and transcribes.
"""

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod

import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from backend.heartbeat import beat

logger = logging.getLogger(__name__)


# How long an entry must have been delivered-but-unacknowledged before recovery
# claims it. A consumer legitimately holds a partial window pending while it fills,
# and a quiet stream can leave one sitting far longer than the window itself, so
# this is deliberately well above any buffering window: claiming audio another
# worker is still accumulating would transcribe it twice.
PENDING_CLAIM_MIN_IDLE_SECONDS = 300

# Entries claimed per XAUTOCLAIM call, and a cap on the number of calls, so a
# pathological backlog cannot stall stream discovery. Anything left is claimed the
# next time this stream is discovered.
RECOVERY_BATCH_SIZE = 200
MAX_RECOVERY_BATCHES = 50


class BaseAudioStreamConsumer(ABC):
    """
    Base class for audio stream consumers.

    Reads from specified stream (client-specific or provider-specific) and transcribes using the provider.
    Writes results to transcription:results:{session_id}.
    """

    def __init__(
        self, provider_name: str, redis_client: redis.Redis, buffer_chunks: int = 30
    ):
        """
        Initialize consumer.

        Dynamically discovers all audio:stream:* streams and uses Redis consumer groups
        for fan-out processing (multiple worker types can process the same stream).

        Args:
            provider_name: Provider name (e.g., "deepgram", "parakeet")
            redis_client: Connected Redis client
            buffer_chunks: Number of chunks to accumulate before transcribing (default: 30 = ~7.5 seconds)
        """
        self.provider_name = provider_name
        self.redis_client = redis_client
        self.buffer_chunks = buffer_chunks

        # Stream configuration
        self.stream_pattern = "audio:stream:*"
        self.group_name = f"{provider_name}_workers"
        self.consumer_name = f"{provider_name}-worker-{os.getpid()}"

        self.running = False

        # Dynamic stream discovery - consumer groups handle fan-out
        self.active_streams = {}  # {stream_name: True}

        # Buffering: accumulate chunks per session
        self.session_buffers = (
            {}
        )  # {session_id: {"chunks": [], "chunk_ids": [], "sample_rate": int}}

    async def discover_streams(self) -> list[str]:
        """
        Discover all audio streams matching the pattern.

        Returns:
            List of stream names
        """
        streams = []
        cursor = b"0"

        while cursor:
            cursor, keys = await self.redis_client.scan(
                cursor, match=self.stream_pattern, count=100
            )
            if keys:
                streams.extend(
                    [k.decode() if isinstance(k, bytes) else k for k in keys]
                )

        return streams

    async def setup_consumer_group(self, stream_name: str):
        """Create consumer group if it doesn't exist."""
        # Create consumer group (ignore error if already exists)
        try:
            await self.redis_client.xgroup_create(
                stream_name, self.group_name, "0", mkstream=True
            )
            logger.debug(
                f"➡️ Created consumer group {self.group_name} for {stream_name}"
            )
        except redis_exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            logger.debug(
                f"➡️ Consumer group {self.group_name} already exists for {stream_name}"
            )

    @abstractmethod
    async def transcribe_audio(self, audio_data: bytes, sample_rate: int) -> dict:
        """
        Transcribe audio using the provider.

        Must be implemented by subclasses.

        Args:
            audio_data: Raw PCM audio bytes
            sample_rate: Audio sample rate (Hz)

        Returns:
            Dict with "text", "words", "segments", "confidence"
        """
        pass

    async def start_consuming(self, heartbeat_name: str | None = None):
        """Discover and consume from multiple streams using Redis consumer groups.

        Args:
            heartbeat_name: If set, beat ``worker:heartbeat:{name}`` once per loop
                iteration so the workers healthcheck can tell this consumer's main
                loop is still turning (not wedged-but-alive).
        """
        self.running = True
        logger.info(
            f"➡️ Starting dynamic stream consumer: {self.consumer_name} (group: {self.group_name})"
        )

        last_discovery = 0
        discovery_interval = 10  # Discover new streams every 10 seconds

        while self.running:
            if heartbeat_name:
                await beat(self.redis_client, heartbeat_name)
            try:
                current_time = time.time()

                # Periodically discover new streams
                if current_time - last_discovery > discovery_interval:
                    discovered = await self.discover_streams()
                    logger.debug(f"🔍 Discovered {len(discovered)} streams")

                    for stream_name in discovered:
                        if stream_name not in self.active_streams:
                            # Setup consumer group for this stream (no manual lock needed)
                            await self.setup_consumer_group(stream_name)
                            # Take over anything a previous incarnation was handed
                            # and never acknowledged, before tailing new entries.
                            # Done here rather than at startup because streams are
                            # discovered continuously, and this is the one moment
                            # per stream when nothing is buffered for it yet.
                            try:
                                await self.recover_pending(stream_name)
                            except Exception as e:  # noqa: BLE001
                                logger.error(
                                    f"➡️ [{self.consumer_name}] Recovery failed for "
                                    f"{stream_name}: {e}",
                                    exc_info=True,
                                )
                            self.active_streams[stream_name] = True
                            logger.info(
                                f"✅ Now consuming from {stream_name} (group: {self.group_name})"
                            )

                    last_discovery = current_time

                # Read from all active streams
                if not self.active_streams:
                    # No streams claimed yet, wait and retry
                    await asyncio.sleep(1)
                    continue

                # Build streams dict for XREADGROUP
                streams_dict = {stream: ">" for stream in self.active_streams.keys()}

                messages = await self.redis_client.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    streams_dict,
                    count=1,
                    block=1000,  # Block for 1 second
                )

                if not messages:
                    continue

                for stream_name, msgs in messages:
                    stream_name_str = (
                        stream_name.decode()
                        if isinstance(stream_name, bytes)
                        else stream_name
                    )
                    for message_id, fields in msgs:
                        await self.process_message(message_id, fields, stream_name_str)

            except redis_exceptions.ResponseError as e:
                error_msg = str(e)

                # Handle NOGROUP errors (stream was deleted or consumer group doesn't exist)
                if "NOGROUP" in error_msg or "no such key" in error_msg.lower():
                    # Extract stream name from error message
                    for stream_name in list(self.active_streams.keys()):
                        if stream_name in error_msg:
                            logger.warning(
                                f"➡️ [{self.consumer_name}] Stream {stream_name} was deleted, removing from active streams"
                            )

                            # Remove from active streams
                            del self.active_streams[stream_name]
                            logger.info(
                                f"➡️ [{self.consumer_name}] Removed {stream_name}, {len(self.active_streams)} streams remaining"
                            )
                            break
                else:
                    # Other ResponseError - log and continue
                    logger.error(f"➡️ [{self.consumer_name}] Redis ResponseError: {e}")

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(
                    f"➡️ [{self.consumer_name}] Error in dynamic consume loop: {e}",
                    exc_info=True,
                )
                await asyncio.sleep(1)

    async def process_message(self, message_id: bytes, fields: dict, stream_name: str):
        """
        Process a single message from the stream.
        Accumulates chunks and transcribes when buffer is full.

        Args:
            message_id: Redis message ID
            fields: Message fields
            stream_name: Stream name this message came from
        """
        try:
            # Extract message data
            audio_data = fields[b"audio_data"]
            session_id = fields[b"session_id"].decode()
            chunk_id = fields[b"chunk_id"].decode()
            sample_rate = int(fields[b"sample_rate"].decode())

            # Check for end-of-session signal
            if chunk_id == "END":
                logger.info(
                    f"➡️ [{self.consumer_name}] {self.provider_name}: Received END signal for session {session_id}"
                )

                # Flush buffer for this session if it has any chunks
                await self.flush_session_buffer(
                    session_id, stream_name, "end-of-session"
                )
                self.session_buffers.pop(session_id, None)

                # ACK the END message
                await self.redis_client.xack(stream_name, self.group_name, message_id)
                return

            # Initialize buffer for this session if needed
            if session_id not in self.session_buffers:
                self.session_buffers[session_id] = {
                    "chunks": [],
                    "chunk_ids": [],
                    "sample_rate": sample_rate,
                    "message_ids": [],
                    "audio_offset_seconds": 0.0,  # Track cumulative audio duration
                }

            # Add to buffer (skip empty audio data from END signals)
            if len(audio_data) > 0:
                buffer = self.session_buffers[session_id]
                buffer["chunks"].append(audio_data)
                buffer["chunk_ids"].append(chunk_id)
                buffer["message_ids"].append(message_id)
            else:
                # ACK and skip empty chunks
                await self.redis_client.xack(stream_name, self.group_name, message_id)
                return

            logger.debug(
                f"➡️ [{self.consumer_name}] {self.provider_name}: Buffered chunk {chunk_id} ({len(buffer['chunks'])}/{self.buffer_chunks})"
            )

            # Transcribe when the window is full. The same flush also runs at
            # end-of-session and on recovery, so all three go through one path.
            if len(buffer["chunks"]) >= self.buffer_chunks:
                await self.flush_session_buffer(session_id, stream_name, "window-full")

        except Exception as e:
            logger.error(
                f"➡️ [{self.consumer_name}] {self.provider_name}: Failed to process chunk {fields.get(b'chunk_id', b'unknown').decode()}: {e}",
                exc_info=True,
            )

    async def flush_session_buffer(
        self, session_id: str, stream_name: str, reason: str
    ) -> int:
        """Transcribe everything buffered for one session and acknowledge it.

        The single place a buffered window is committed, reached from three
        directions: the window filling, the end-of-session marker, and recovery of
        a window a previous incarnation left behind. Acknowledgement happens only
        after :meth:`store_result` returns, so a failure anywhere leaves the entries
        pending and the audio recoverable rather than silently dropped.

        Returns the number of messages acknowledged.
        """
        buffer = self.session_buffers.get(session_id)
        if not buffer or not buffer["chunks"]:
            return 0

        start_time = time.time()
        combined_audio = b"".join(buffer["chunks"])
        combined_chunk_id = f"{buffer['chunk_ids'][0]}-{buffer['chunk_ids'][-1]}"
        sample_rate = buffer["sample_rate"]
        audio_offset = buffer["audio_offset_seconds"]
        audio_duration_seconds = len(combined_audio) / (sample_rate * 2)

        logger.info(
            f"➡️ [{self.consumer_name}] {self.provider_name}: Flushing "
            f"{len(buffer['chunks'])} chunks ({len(combined_audio)} bytes, "
            f"{audio_duration_seconds:.1f}s, offset={audio_offset:.1f}s) as "
            f"{combined_chunk_id} [{reason}]"
        )

        result = await self.transcribe_audio(combined_audio, sample_rate)

        # Timestamps arrive relative to the window; shift them onto the session
        # clock. The end-of-session flush used to skip this, so the final partial
        # window's words restarted from zero and collided with the opening of the
        # conversation.
        adjusted_segments = []
        for seg in result.get("segments", []):
            adjusted = seg.copy()
            adjusted["start"] = seg.get("start", 0.0) + audio_offset
            adjusted["end"] = seg.get("end", 0.0) + audio_offset
            adjusted_segments.append(adjusted)
        adjusted_words = []
        for word in result.get("words", []):
            adjusted = word.copy()
            adjusted["start"] = word.get("start", 0.0) + audio_offset
            adjusted["end"] = word.get("end", 0.0) + audio_offset
            adjusted_words.append(adjusted)

        processing_time = time.time() - start_time
        await self.store_result(
            session_id=session_id,
            chunk_id=combined_chunk_id,
            text=result.get("text", ""),
            confidence=result.get("confidence", 0.0),
            words=adjusted_words,
            segments=adjusted_segments,
            processing_time=processing_time,
        )

        acked = 0
        for msg_id in buffer["message_ids"]:
            await self.redis_client.xack(stream_name, self.group_name, msg_id)
            acked += 1

        buffer["audio_offset_seconds"] += audio_duration_seconds
        buffer["chunks"] = []
        buffer["chunk_ids"] = []
        buffer["message_ids"] = []

        logger.info(
            f"➡️ [{self.consumer_name}] {self.provider_name}: Completed "
            f"{combined_chunk_id} in {processing_time:.2f}s (transcript: "
            f"{len(result.get('text', ''))} chars, acked={acked}, "
            f"next_offset={buffer['audio_offset_seconds']:.1f}s)"
        )
        return acked

    async def recover_pending(self, stream_name: str) -> int:
        """Reprocess entries this group was handed but never acknowledged.

        ``XREADGROUP`` records every delivery in the consumer's pending list and
        keeps it there until ``XACK`` — precisely so a consumer that stops mid-work
        loses nothing. The main loop only ever reads ``">"`` (undelivered messages),
        so nothing ever looked at that list. A process buffering a partial window
        when it stopped left those entries pending forever: invisible to every later
        incarnation, the audio never transcribed, and the stream never reclaimable
        because its group never drains. One stream here held 119 chunks — 30 seconds
        of audio and 140 MB of log — in exactly that state.

        ``XAUTOCLAIM`` rather than a ``"0"`` replay because the consumer name embeds
        the pid, so a restarted worker is usually a *different* consumer and ``"0"``
        would only return its own (empty) backlog. Claiming takes ownership from
        whichever consumer held it, alive or long gone.

        Only entries idle beyond :data:`PENDING_CLAIM_MIN_IDLE_SECONDS` are claimed.
        A consumer legitimately holds a partial window unacknowledged while it
        fills, so a shorter threshold would let one worker claim audio another is
        actively accumulating and transcribe it twice.
        """
        min_idle_ms = int(PENDING_CLAIM_MIN_IDLE_SECONDS * 1000)
        cursor = "0-0"
        recovered_sessions: set[str] = set()
        seen: set[bytes] = set()
        claimed = 0

        for _ in range(MAX_RECOVERY_BATCHES):
            response = await self.redis_client.xautoclaim(
                stream_name,
                self.group_name,
                self.consumer_name,
                min_idle_ms,
                start_id=cursor,
                count=RECOVERY_BATCH_SIZE,
            )
            cursor, messages = response[0], response[1]
            # Claimed entries stay pending until the window they land in is
            # committed, so a cursor that has not advanced hands back the same
            # entries. Stopping on "nothing new" terminates on both a completed
            # scan (cursor "0-0") and a cursor that merely repeats itself.
            fresh = [(mid, fields) for mid, fields in messages if mid not in seen]
            if not fresh:
                break
            for message_id, fields in fresh:
                seen.add(message_id)
                claimed += 1
                session_field = fields.get(b"session_id")
                if session_field:
                    recovered_sessions.add(session_field.decode())
                await self.process_message(message_id, fields, stream_name)

        # A recovered window is usually short of full — that is why it was stranded
        # in the first place — so it would simply sit in the buffer and go pending
        # again. Committing it here is what actually drains the group.
        for session_id in recovered_sessions:
            try:
                await self.flush_session_buffer(session_id, stream_name, "recovery")
            except Exception as e:  # noqa: BLE001
                # Leave the entries pending. Unacknowledged audio is recoverable on
                # the next pass; acknowledging it here would discard it.
                logger.error(
                    f"➡️ [{self.consumer_name}] Recovery flush failed for session "
                    f"{session_id}: {e}",
                    exc_info=True,
                )

        if claimed:
            logger.warning(
                f"➡️ [{self.consumer_name}] {self.provider_name}: Recovered "
                f"{claimed} unacknowledged entries from {stream_name} across "
                f"{len(recovered_sessions)} session(s)"
            )
        return claimed

    async def store_result(
        self,
        session_id: str,
        chunk_id: str,
        text: str,
        confidence: float,
        words: list,
        segments: list,
        processing_time: float,
    ):
        """
        Store transcription result in Redis Stream.

        Args:
            session_id: Session identifier
            chunk_id: Chunk identifier
            text: Transcribed text
            confidence: Confidence score
            words: Word-level data
            segments: Speaker segments
            processing_time: Processing time in seconds
        """
        result_data = {
            b"text": text.encode(),
            b"chunk_id": chunk_id.encode(),
            b"provider": self.provider_name.encode(),
            b"confidence": str(confidence).encode(),
            b"processing_time": str(processing_time).encode(),
            b"timestamp": str(time.time()).encode(),
        }

        # Add optional JSON fields
        if words:
            result_data[b"words"] = json.dumps(words).encode()
        if segments:
            result_data[b"segments"] = json.dumps(segments).encode()

        # Write to session results stream with MAXLEN limit
        session_results_stream = f"transcription:results:{session_id}"
        message_id = await self.redis_client.xadd(
            session_results_stream,
            result_data,
            maxlen=1000,  # Keep max 1k results per session
            approximate=True,
        )

        logger.debug(
            f"➡️ Stored result {chunk_id} in {session_results_stream}: "
            f"text_len={len(text)}, msg_id={message_id.decode()}"
        )

    async def stop(self):
        """Stop consuming messages."""
        self.running = False
        logger.info(f"➡️ Stopping consumer: {self.consumer_name}")
