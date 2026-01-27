"""
Generic streaming transcription consumer for real-time audio processing.

Uses registry-driven transcription provider from config.yml (supports any streaming provider).

Reads from: audio:stream:* streams
Publishes interim to: Redis Pub/Sub channel transcription:interim:{session_id}
Writes final to: transcription:results:{session_id} Redis Stream
Triggers plugins: streaming_transcript level (final results only)
"""

import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional

import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from advanced_omi_backend.plugins.router import PluginRouter
from advanced_omi_backend.services.transcription import get_transcription_provider
from advanced_omi_backend.client_manager import get_client_owner_async

logger = logging.getLogger(__name__)


class StreamingTranscriptionConsumer:
    """
    Generic streaming transcription consumer using registry-driven providers.

    - Discovers audio:stream:* streams dynamically
    - Uses Redis consumer groups for fan-out (allows batch workers to process same stream)
    - Starts WebSocket connections using configured provider (from config.yml)
    - Sends audio immediately (no buffering)
    - Publishes interim results to Redis Pub/Sub for client display
    - Publishes final results to Redis Streams for storage
    - Triggers plugins only on final results

    Supported providers (via config.yml): Any streaming STT service with WebSocket API
    """

    def __init__(self, redis_client: redis.Redis, plugin_router: Optional[PluginRouter] = None):
        """
        Initialize streaming transcription consumer.

        Args:
            redis_client: Connected Redis client
            plugin_router: Plugin router for triggering plugins on final results
        """
        self.redis_client = redis_client
        self.plugin_router = plugin_router

        # Get streaming transcription provider from registry
        self.provider = get_transcription_provider(mode="streaming")
        if not self.provider:
            raise RuntimeError(
                "Failed to load streaming transcription provider. "
                "Ensure config.yml has a default 'stt_stream' model configured."
            )

        # Stream configuration
        self.stream_pattern = "audio:stream:*"
        self.group_name = "streaming-transcription"
        self.consumer_name = f"streaming-worker-{os.getpid()}"

        self.running = False

        # Active stream tracking - consumer groups handle fan-out
        self.active_streams: Dict[str, Dict] = {}  # {stream_name: {"session_id": ...}}

        # Session tracking for WebSocket connections
        self.active_sessions: Dict[str, Dict] = {}  # {session_id: {"last_activity": timestamp}}

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
                streams.extend([k.decode() if isinstance(k, bytes) else k for k in keys])

        return streams

    async def setup_consumer_group(self, stream_name: str):
        """Create consumer group if it doesn't exist."""
        try:
            await self.redis_client.xgroup_create(
                stream_name,
                self.group_name,
                "0",
                mkstream=True
            )
            logger.debug(f"➡️ Created consumer group {self.group_name} for {stream_name}")
        except redis_exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            logger.debug(f"➡️ Consumer group {self.group_name} already exists for {stream_name}")

    async def start_session_stream(self, session_id: str, sample_rate: int = 16000):
        """
        Start WebSocket connection to Deepgram for a session.

        Args:
            session_id: Session ID (client_id from audio stream)
            sample_rate: Audio sample rate in Hz
        """
        try:
            await self.provider.start_stream(
                client_id=session_id,
                sample_rate=sample_rate,
                diarize=False  # Deepgram streaming doesn't support diarization
            )

            self.active_sessions[session_id] = {
                "last_activity": time.time(),
                "sample_rate": sample_rate,
                "audio_offset_seconds": 0.0  # Track cumulative audio duration for timestamp adjustment
            }

            logger.info(f"🎙️ Started Deepgram WebSocket stream for session: {session_id}")

        except Exception as e:
            logger.error(f"Failed to start Deepgram stream for {session_id}: {e}", exc_info=True)

            # Set error flag in Redis so speech detection can detect failure early
            session_key = f"audio:session:{session_id}"
            try:
                await self.redis_client.hset(session_key, "transcription_error", str(e))
                logger.info(f"Set transcription error flag for {session_id}")
            except Exception as redis_error:
                logger.warning(f"Failed to set error flag in Redis: {redis_error}")

            raise

    async def end_session_stream(self, session_id: str):
        """
        End WebSocket connection to Deepgram for a session.

        Args:
            session_id: Session ID
        """
        try:
            # Get final result from Deepgram
            final_result = await self.provider.end_stream(client_id=session_id)

            # If there's a final result, publish it
            if final_result and final_result.get("text"):
                await self.publish_to_client(session_id, final_result, is_final=True)
                await self.store_final_result(session_id, final_result)

                # Trigger plugins on final result
                if self.plugin_router:
                    await self.trigger_plugins(session_id, final_result)

            self.active_sessions.pop(session_id, None)
            logger.info(f"🛑 Ended Deepgram WebSocket stream for session: {session_id}")

        except Exception as e:
            logger.error(f"Error ending stream for {session_id}: {e}", exc_info=True)

    async def process_audio_chunk(self, session_id: str, audio_chunk: bytes, chunk_id: str):
        """
        Process a single audio chunk through Deepgram WebSocket.

        Args:
            session_id: Session ID
            audio_chunk: Raw audio bytes
            chunk_id: Chunk identifier from Redis stream
        """
        try:
            # Send audio chunk to Deepgram WebSocket and get result
            result = await self.provider.process_audio_chunk(
                client_id=session_id,
                audio_chunk=audio_chunk
            )

            # Update last activity
            if session_id in self.active_sessions:
                self.active_sessions[session_id]["last_activity"] = time.time()

            # Deepgram returns None if no response yet, or a dict with results
            if result:
                is_final = result.get("is_final", False)
                text = result.get("text", "")
                word_count = len(result.get("words", []))

                # Track transcript at each step
                logger.info(
                    f"🔤 TRANSCRIPT [DEEPGRAM] session={session_id}, is_final={is_final}, "
                    f"words={word_count}, text=\"{text}\""
                )

                # Always publish to clients (interim + final) for real-time display
                await self.publish_to_client(session_id, result, is_final=is_final)

                # If final result, also store and trigger plugins
                if is_final:
                    logger.info(
                        f"🔤 TRANSCRIPT [STORE] session={session_id}, words={word_count}, text=\"{text}\""
                    )
                    await self.store_final_result(session_id, result, chunk_id=chunk_id)

                    # Trigger plugins on final results only
                    if self.plugin_router:
                        await self.trigger_plugins(session_id, result)

        except Exception as e:
            logger.error(f"Error processing audio chunk for {session_id}: {e}", exc_info=True)

    async def publish_to_client(self, session_id: str, result: Dict, is_final: bool):
        """
        Publish interim or final results to Redis Pub/Sub for client consumption.

        Args:
            session_id: Session ID
            result: Transcription result from Deepgram
            is_final: Whether this is a final result
        """
        try:
            channel = f"transcription:interim:{session_id}"

            # Prepare message for clients
            message = {
                "text": result.get("text", ""),
                "is_final": is_final,
                "words": result.get("words", []),
                "confidence": result.get("confidence", 0.0),
                "timestamp": time.time()
            }

            # Publish to Redis Pub/Sub
            await self.redis_client.publish(channel, json.dumps(message))

            result_type = "FINAL" if is_final else "interim"
            logger.debug(f"📢 Published {result_type} result to {channel}: {message['text'][:50]}...")

        except Exception as e:
            logger.error(f"Error publishing to client for {session_id}: {e}", exc_info=True)

    async def store_final_result(self, session_id: str, result: Dict, chunk_id: str = None):
        """
        Store final transcription result to Redis Stream with cumulative timestamp adjustment.

        Transcription providers return word timestamps that reset to 0 for each chunk.
        We maintain a running audio_offset_seconds to make timestamps cumulative across
        the session, enabling accurate speech duration calculation for speech detection.

        Args:
            session_id: Session ID
            result: Final transcription result
            chunk_id: Optional chunk identifier
        """
        try:
            stream_name = f"transcription:results:{session_id}"

            # Get cumulative audio offset for this session
            audio_offset = 0.0
            chunk_duration = 0.0
            if session_id in self.active_sessions:
                audio_offset = self.active_sessions[session_id].get("audio_offset_seconds", 0.0)

            # Adjust word timestamps by cumulative offset
            words = result.get("words", [])
            adjusted_words = []
            if words:
                for word in words:
                    adjusted_word = word.copy()
                    adjusted_word["start"] = word.get("start", 0.0) + audio_offset
                    adjusted_word["end"] = word.get("end", 0.0) + audio_offset
                    adjusted_words.append(adjusted_word)

                # Calculate chunk duration from last word's end time
                if adjusted_words:
                    last_word_end = words[-1].get("end", 0.0)  # Use unadjusted for duration calc
                    chunk_duration = last_word_end

                logger.debug(f"➡️ [STREAMING] Adjusted {len(adjusted_words)} words by +{audio_offset:.1f}s (chunk_duration={chunk_duration:.1f}s)")

            # Adjust segment timestamps too
            segments = result.get("segments", [])
            adjusted_segments = []
            if segments:
                for seg in segments:
                    adjusted_seg = seg.copy()
                    adjusted_seg["start"] = seg.get("start", 0.0) + audio_offset
                    adjusted_seg["end"] = seg.get("end", 0.0) + audio_offset
                    adjusted_segments.append(adjusted_seg)

            # Prepare result entry - MUST match aggregator's expected schema
            # All keys and values must be bytes to match consumer.py format
            entry = {
                b"text": result.get("text", "").encode(),
                b"chunk_id": (chunk_id or f"final_{int(time.time() * 1000)}").encode(),
                b"provider": b"deepgram-stream",
                b"confidence": str(result.get("confidence", 0.0)).encode(),
                b"processing_time": b"0.0",  # Streaming has minimal processing time
                b"timestamp": str(time.time()).encode(),
            }

            # Add adjusted JSON fields
            if adjusted_words:
                entry[b"words"] = json.dumps(adjusted_words).encode()

            if adjusted_segments:
                entry[b"segments"] = json.dumps(adjusted_segments).encode()

            # Write to Redis Stream
            await self.redis_client.xadd(stream_name, entry)

            # Update cumulative offset for next chunk
            if session_id in self.active_sessions and chunk_duration > 0:
                self.active_sessions[session_id]["audio_offset_seconds"] += chunk_duration
                new_offset = self.active_sessions[session_id]["audio_offset_seconds"]
                logger.info(f"💾 Stored final result to {stream_name}: {result.get('text', '')[:50]}... (offset: {audio_offset:.1f}s → {new_offset:.1f}s)")
            else:
                logger.info(f"💾 Stored final result to {stream_name}: {result.get('text', '')[:50]}...")

        except Exception as e:
            logger.error(f"Error storing final result for {session_id}: {e}", exc_info=True)

    async def _get_user_id_from_client_id(self, client_id: str) -> Optional[str]:
        """
        Look up user_id from client_id using ClientManager (async Redis lookup).

        Args:
            client_id: Client ID to search for

        Returns:
            user_id if found, None otherwise
        """
        user_id = await get_client_owner_async(client_id)

        if user_id:
            logger.debug(f"Found user_id {user_id} for client_id {client_id} via Redis")
        else:
            logger.warning(f"No user_id found for client_id {client_id} in Redis")

        return user_id

    async def trigger_plugins(self, session_id: str, result: Dict):
        """
        Trigger plugins at streaming_transcript access level (final results only).

        Args:
            session_id: Session ID (client_id from stream name)
            result: Final transcription result
        """
        try:
            # Find user_id by looking up session with matching client_id
            # session_id here is actually the client_id extracted from stream name
            user_id = await self._get_user_id_from_client_id(session_id)

            if not user_id:
                logger.warning(
                    f"Could not find user_id for client_id {session_id}. "
                    "Plugins will not be triggered."
                )
                return

            plugin_data = {
                'transcript': result.get("text", ""),
                'session_id': session_id,
                'words': result.get("words", []),
                'segments': result.get("segments", []),
                'confidence': result.get("confidence", 0.0),
                'is_final': True
            }

            # Dispatch transcript.streaming event
            logger.info(f"🎯 Dispatching transcript.streaming event for user {user_id}, transcript: {plugin_data['transcript'][:50]}...")

            plugin_results = await self.plugin_router.dispatch_event(
                event='transcript.streaming',
                user_id=user_id,
                data=plugin_data,
                metadata={'client_id': session_id}
            )

            if plugin_results:
                logger.info(f"✅ Plugins triggered successfully: {len(plugin_results)} results")
            else:
                logger.info(f"ℹ️ No plugins triggered (no matching conditions)")

        except Exception as e:
            logger.error(f"Error triggering plugins for {session_id}: {e}", exc_info=True)

    async def process_stream(self, stream_name: str):
        """
        Process a single audio stream.

        Args:
            stream_name: Redis stream name (e.g., "audio:stream:user01-phone")
        """
        # Extract session_id from stream name (format: audio:stream:{session_id})
        session_id = stream_name.replace("audio:stream:", "")

        # Track this stream
        self.active_streams[stream_name] = {
            "session_id": session_id,
            "started_at": time.time()
        }

        # Start WebSocket connection to Deepgram
        await self.start_session_stream(session_id)

        last_id = "0"  # Start from beginning
        stream_ended = False

        try:
            while self.running and not stream_ended:
                # Read messages from Redis stream using consumer group
                try:
                    messages = await self.redis_client.xreadgroup(
                        self.group_name,  # "streaming-transcription"
                        self.consumer_name,  # "streaming-worker-{pid}"
                        {stream_name: ">"},  # Read only new messages
                        count=10,
                        block=1000  # Block for 1 second
                    )

                    if not messages:
                        # No new messages - check if stream is still alive
                        # Check for stream end marker or timeout
                        if session_id not in self.active_sessions:
                            logger.info(f"Session {session_id} no longer active, ending stream processing")
                            stream_ended = True
                        continue

                    for stream, stream_messages in messages:
                        logger.debug(f"📥 Read {len(stream_messages)} messages from {stream_name}")
                        for message_id, fields in stream_messages:
                            msg_id = message_id.decode() if isinstance(message_id, bytes) else message_id

                            # Check for end marker
                            if fields.get(b'end_marker') or fields.get('end_marker'):
                                logger.info(f"End marker received for {session_id}")
                                stream_ended = True
                                # ACK the end marker
                                await self.redis_client.xack(stream_name, self.group_name, msg_id)
                                break

                            # Extract audio data (producer sends as 'audio_data', not 'audio_chunk')
                            audio_chunk = fields.get(b'audio_data') or fields.get('audio_data')
                            if audio_chunk:
                                logger.debug(f"🎵 Processing audio chunk {msg_id} ({len(audio_chunk)} bytes)")
                                # Process audio chunk through Deepgram WebSocket
                                await self.process_audio_chunk(
                                    session_id=session_id,
                                    audio_chunk=audio_chunk,
                                    chunk_id=msg_id
                                )
                            else:
                                logger.warning(f"⚠️ Message {msg_id} has no audio_data field")

                            # ACK the message after processing
                            await self.redis_client.xack(stream_name, self.group_name, msg_id)

                        if stream_ended:
                            break

                except redis_exceptions.ResponseError as e:
                    if "NOGROUP" in str(e):
                        # Stream has expired or been deleted - exit gracefully
                        logger.info(f"Stream {stream_name} expired or deleted, ending processing")
                        stream_ended = True
                        break
                    else:
                        logger.error(f"Redis error reading from stream {stream_name}: {e}", exc_info=True)
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error reading from stream {stream_name}: {e}", exc_info=True)
                    await asyncio.sleep(1)

        finally:
            # End WebSocket connection
            await self.end_session_stream(session_id)

            # Remove from active streams tracking
            self.active_streams.pop(stream_name, None)
            logger.debug(f"Removed {stream_name} from active streams tracking")

    async def start_consuming(self):
        """
        Start consuming audio streams and processing through Deepgram WebSocket.
        Uses Redis consumer groups for fan-out (allows batch workers to process same stream).
        """
        self.running = True
        logger.info(f"🚀 Deepgram streaming consumer started (group: {self.group_name})")

        try:
            while self.running:
                # Discover available streams
                streams = await self.discover_streams()

                if streams:
                    logger.debug(f"🔍 Discovered {len(streams)} audio streams")
                else:
                    logger.debug("🔍 No audio streams found")

                # Setup consumer groups and spawn processing tasks
                for stream_name in streams:
                    if stream_name in self.active_streams:
                        continue  # Already processing

                    # Setup consumer group (no manual lock needed)
                    await self.setup_consumer_group(stream_name)

                    # Track stream and spawn task to process it
                    session_id = stream_name.replace("audio:stream:", "")
                    self.active_streams[stream_name] = {"session_id": session_id}

                    # Spawn task to process this stream
                    asyncio.create_task(self.process_stream(stream_name))
                    logger.info(f"✅ Now consuming from {stream_name} (group: {self.group_name})")

                # Sleep before next discovery cycle (1s for fast discovery)
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Fatal error in consumer main loop: {e}", exc_info=True)
        finally:
            await self.stop()

    async def stop(self):
        """Stop consuming and clean up resources."""
        logger.info("🛑 Stopping Deepgram streaming consumer...")
        self.running = False

        # End all active sessions
        session_ids = list(self.active_sessions.keys())
        for session_id in session_ids:
            try:
                await self.end_session_stream(session_id)
            except Exception as e:
                logger.error(f"Error ending session {session_id}: {e}")

        logger.info("✅ Deepgram streaming consumer stopped")
