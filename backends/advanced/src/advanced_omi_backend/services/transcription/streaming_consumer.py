"""
Generic streaming transcription consumer for real-time audio processing.

Uses registry-driven transcription provider from config.yml (supports any streaming provider).

Reads from: audio:stream:* streams
Publishes interim to: Redis Pub/Sub channel transcription:interim:{session_id}
Writes final to: transcription:results:{session_id} Redis Stream
Triggers plugins: streaming_transcript level (final results only)
Identifies speakers: on final results via speaker recognition service
"""

import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional

import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from advanced_omi_backend.client_manager import get_client_owner_async
from advanced_omi_backend.models.user import get_user_by_id
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.plugins.router import PluginRouter
from advanced_omi_backend.services.transcription import get_transcription_provider
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
from advanced_omi_backend.utils.audio_utils import pcm_to_wav_bytes

logger = logging.getLogger(__name__)


def _normalize_words(words: list) -> None:
    """Normalize provider-specific word field names in-place.

    Waves uses ``start_time``/``end_time`` while the internal format uses
    ``start``/``end``.  This copies values so downstream code can rely on
    the canonical field names.
    """
    for w in words:
        if not isinstance(w, dict):
            continue
        if "start" not in w and "start_time" in w:
            w["start"] = w["start_time"]
        if "end" not in w and "end_time" in w:
            w["end"] = w["end_time"]


def _group_words_into_segments(words: list) -> list:
    """Group consecutive words by speaker ID into segment dicts.

    Each segment contains:
    - ``start`` / ``end``: time span
    - ``text``: concatenated word text
    - ``speaker``: "Speaker N" string
    - ``words``: the original word dicts belonging to this segment

    Words without a speaker field are assigned to speaker -1.
    """
    if not words:
        return []

    segments: list = []
    current_speaker = None
    current_words: list = []

    for w in words:
        if not isinstance(w, dict):
            continue
        spk = w.get("speaker", -1)
        if spk is None:
            spk = -1

        if spk != current_speaker and current_words:
            # Flush previous segment
            segments.append(
                {
                    "start": current_words[0].get("start", 0.0),
                    "end": current_words[-1].get("end", 0.0),
                    "text": " ".join(cw.get("word", "") for cw in current_words),
                    "speaker": (
                        f"Speaker {current_speaker}"
                        if current_speaker != -1
                        else "Unknown"
                    ),
                    "words": list(current_words),
                }
            )
            current_words = []

        current_speaker = spk
        current_words.append(w)

    # Flush last segment
    if current_words:
        segments.append(
            {
                "start": current_words[0].get("start", 0.0),
                "end": current_words[-1].get("end", 0.0),
                "text": " ".join(cw.get("word", "") for cw in current_words),
                "speaker": (
                    f"Speaker {current_speaker}" if current_speaker != -1 else "Unknown"
                ),
                "words": list(current_words),
            }
        )

    return segments


class StreamingTranscriptionConsumer:
    """
    Generic streaming transcription consumer using registry-driven providers.

    - Discovers audio:stream:* streams dynamically
    - Uses Redis consumer groups for fan-out (allows batch workers to process same stream)
    - Starts WebSocket connections using configured provider (from config.yml)
    - Sends audio immediately (no buffering)
    - Publishes interim results to Redis Pub/Sub for client display
    - Publishes final results to Redis Streams for storage
    - Identifies speakers on final results via speaker recognition service
    - Gates plugin dispatch on primary speaker configuration
    - Triggers plugins only on final results

    Supported providers (via config.yml): Any streaming STT service with WebSocket API
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        plugin_router: Optional[PluginRouter] = None,
        speaker_client: Optional[SpeakerRecognitionClient] = None,
    ):
        """
        Initialize streaming transcription consumer.

        Args:
            redis_client: Connected Redis client
            plugin_router: Plugin router for triggering plugins on final results
            speaker_client: Speaker recognition client for identifying speakers
        """
        self.redis_client = redis_client
        self.plugin_router = plugin_router
        self.speaker_client = speaker_client

        # Get streaming transcription provider from registry
        self.provider = get_transcription_provider(mode="streaming")
        if not self.provider:
            raise RuntimeError(
                "Failed to load streaming transcription provider. "
                "Ensure config.yml has a default 'stt_stream' model configured."
            )

        # Check if provider supports streaming diarization
        self._provider_has_diarization = (
            hasattr(self.provider, "capabilities")
            and "diarization" in self.provider.capabilities
        )

        # Stream configuration
        self.stream_pattern = "audio:stream:*"
        self.group_name = "streaming-transcription"
        self.consumer_name = f"streaming-worker-{os.getpid()}"

        self.running = False

        # Active stream tracking - consumer groups handle fan-out
        self.active_streams: Dict[str, Dict] = {}  # {stream_name: {"session_id": ...}}

        # Session tracking for WebSocket connections
        self.active_sessions: Dict[str, Dict] = (
            {}
        )  # {session_id: {"last_activity": timestamp}}

        # Audio buffers for speaker identification (raw PCM bytes per session)
        self._audio_buffers: Dict[str, bytearray] = {}

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
        try:
            await self.redis_client.xgroup_create(
                stream_name, self.group_name, "0", mkstream=True
            )
            logger.debug(f"Created consumer group {self.group_name} for {stream_name}")
        except redis_exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            logger.debug(
                f"Consumer group {self.group_name} already exists for {stream_name}"
            )

    async def start_session_stream(self, session_id: str, sample_rate: int = 16000):
        """
        Start WebSocket connection to transcription provider for a session.

        Args:
            session_id: Session ID (client_id from audio stream)
            sample_rate: Audio sample rate in Hz
        """
        try:
            await self.provider.start_stream(
                client_id=session_id,
                sample_rate=sample_rate,
                diarize=self._provider_has_diarization,
            )

            self.active_sessions[session_id] = {
                "last_activity": time.time(),
                "sample_rate": sample_rate,
            }

            # Only buffer audio for speaker identification when provider lacks diarization
            if not self._provider_has_diarization:
                self._audio_buffers[session_id] = bytearray()

            logger.info(f"Started streaming transcription for session: {session_id}")

        except Exception as e:
            logger.error(f"Failed to start stream for {session_id}: {e}", exc_info=True)

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
        End WebSocket connection to transcription provider for a session.

        Args:
            session_id: Session ID
        """
        try:
            # Get final result from provider
            final_result = await self.provider.end_stream(client_id=session_id)

            # If there's a final result, publish it
            if final_result and final_result.get("text"):
                words = final_result.get("words", [])
                _normalize_words(words)

                # Check if words carry per-word speaker labels (provider diarization)
                has_word_speakers = (
                    self._provider_has_diarization
                    and words
                    and any(
                        isinstance(w, dict) and w.get("speaker") is not None
                        for w in words
                    )
                )

                if has_word_speakers:
                    final_result["segments"] = _group_words_into_segments(words)
                    speaker_name = None
                    speaker_confidence = 0.0
                else:
                    speaker_name, speaker_confidence = await self._identify_speaker(
                        session_id
                    )

                if speaker_name:
                    final_result["speaker_name"] = speaker_name
                    final_result["speaker_confidence"] = speaker_confidence

                await self.publish_to_client(
                    session_id,
                    final_result,
                    is_final=True,
                    speaker_name=speaker_name,
                    speaker_confidence=speaker_confidence,
                )
                await self.store_final_result(session_id, final_result)

                # Trigger plugins on final result
                if self.plugin_router:
                    await self.trigger_plugins(
                        session_id, final_result, speaker_name=speaker_name
                    )

            self.active_sessions.pop(session_id, None)
            self._audio_buffers.pop(session_id, None)

            # Signal that streaming transcription is complete for this session
            completion_key = f"transcription:complete:{session_id}"
            await self.redis_client.set(completion_key, "1", ex=300)  # 5 min TTL
            logger.info(
                f"Streaming transcription complete for {session_id} (signal set)"
            )

        except Exception as e:
            logger.error(f"Error ending stream for {session_id}: {e}", exc_info=True)
            # Still signal completion even on error so conversation job doesn't hang
            try:
                completion_key = f"transcription:complete:{session_id}"
                await self.redis_client.set(completion_key, "error", ex=300)
                logger.warning(f"Set error completion signal for {session_id}")
            except Exception:
                pass  # Best effort

    async def process_audio_chunk(
        self, session_id: str, audio_chunk: bytes, chunk_id: str
    ):
        """
        Process a single audio chunk through streaming transcription provider.

        Args:
            session_id: Session ID
            audio_chunk: Raw audio bytes
            chunk_id: Chunk identifier from Redis stream
        """
        try:
            # Buffer audio for speaker identification (only when provider lacks diarization)
            if not self._provider_has_diarization and session_id in self._audio_buffers:
                self._audio_buffers[session_id].extend(audio_chunk)

            # Send audio chunk to provider WebSocket and get result
            result = await self.provider.process_audio_chunk(
                client_id=session_id, audio_chunk=audio_chunk
            )

            # Update last activity
            if session_id in self.active_sessions:
                self.active_sessions[session_id]["last_activity"] = time.time()

            # Provider returns None if no response yet, or a dict with results
            if result:
                is_final = result.get("is_final", False)
                text = result.get("text", "")
                words = result.get("words", [])
                word_count = len(words)

                # Normalize provider-specific word field names (e.g. start_time → start)
                _normalize_words(words)

                # Track transcript at each step
                logger.info(
                    f"TRANSCRIPT session={session_id}, is_final={is_final}, "
                    f'words={word_count}, text="{text}"'
                )

                if is_final:
                    # Check if words carry per-word speaker labels (provider diarization)
                    has_word_speakers = (
                        self._provider_has_diarization
                        and words
                        and any(
                            isinstance(w, dict) and w.get("speaker") is not None
                            for w in words
                        )
                    )

                    if has_word_speakers:
                        # Build segments from per-word speaker labels
                        result["segments"] = _group_words_into_segments(words)
                        speaker_name = None
                        speaker_confidence = 0.0
                    else:
                        # Identify speaker from buffered audio (non-diarizing providers)
                        speaker_name, speaker_confidence = await self._identify_speaker(
                            session_id
                        )

                    if speaker_name:
                        result["speaker_name"] = speaker_name
                        result["speaker_confidence"] = speaker_confidence

                    # Publish to clients with speaker info
                    await self.publish_to_client(
                        session_id,
                        result,
                        is_final=True,
                        speaker_name=speaker_name,
                        speaker_confidence=speaker_confidence,
                    )

                    logger.info(
                        f"TRANSCRIPT [STORE] session={session_id}, words={word_count}, "
                        f"speaker={speaker_name}, segments={len(result.get('segments', []))}, "
                        f'text="{text}"'
                    )
                    await self.store_final_result(session_id, result, chunk_id=chunk_id)

                    # Trigger plugins on final results only
                    if self.plugin_router:
                        await self.trigger_plugins(
                            session_id, result, speaker_name=speaker_name
                        )
                else:
                    # Interim result — normalize words but no speaker identification
                    await self.publish_to_client(session_id, result, is_final=False)

        except Exception as e:
            logger.error(
                f"Error processing audio chunk for {session_id}: {e}", exc_info=True
            )

    async def _identify_speaker(self, session_id: str) -> tuple[Optional[str], float]:
        """Identify the speaker from buffered audio via speaker recognition service.

        Args:
            session_id: Session ID to get buffered audio for

        Returns:
            Tuple of (speaker_name, confidence). (None, 0.0) if unavailable.
        """
        if not self.speaker_client or not self.speaker_client.enabled:
            return None, 0.0

        buffer = self._audio_buffers.get(session_id)
        if not buffer or len(buffer) < 3200:  # Less than 0.1s of 16kHz 16-bit mono
            return None, 0.0

        try:
            # Resolve user_id for speaker scoping
            user_id = await self._get_user_id_from_client_id(session_id)

            # Convert buffered PCM to WAV
            wav_bytes = pcm_to_wav_bytes(
                bytes(buffer), sample_rate=16000, channels=1, sample_width=2
            )

            # Call speaker recognition service
            result = await self.speaker_client.identify_segment(
                audio_wav_bytes=wav_bytes,
                user_id=user_id,
            )

            if result.get("found"):
                speaker_name = result.get("speaker_name", "")
                confidence = result.get("confidence", 0.0)
                logger.info(
                    f"Speaker identified for {session_id}: {speaker_name} "
                    f"(confidence={confidence:.2f})"
                )
                return speaker_name, confidence

            return None, 0.0

        except Exception as e:
            logger.warning(f"Speaker identification failed for {session_id}: {e}")
            return None, 0.0
        finally:
            # Clear the buffer after identification attempt
            if session_id in self._audio_buffers:
                self._audio_buffers[session_id] = bytearray()

    async def publish_to_client(
        self,
        session_id: str,
        result: Dict,
        is_final: bool,
        speaker_name: Optional[str] = None,
        speaker_confidence: float = 0.0,
    ):
        """
        Publish interim or final results to Redis Pub/Sub for client consumption.

        Args:
            session_id: Session ID
            result: Transcription result
            is_final: Whether this is a final result
            speaker_name: Identified speaker name (final results only)
            speaker_confidence: Speaker identification confidence
        """
        try:
            channel = f"transcription:interim:{session_id}"

            # Prepare message for clients
            message = {
                "text": result.get("text", ""),
                "is_final": is_final,
                "words": result.get("words", []),
                "segments": result.get("segments", []),
                "confidence": result.get("confidence", 0.0),
                "timestamp": time.time(),
            }

            # Include speaker info on final results
            if is_final and speaker_name:
                message["speaker_name"] = speaker_name
                message["speaker_confidence"] = speaker_confidence

            # Publish to Redis Pub/Sub
            await self.redis_client.publish(channel, json.dumps(message))

            result_type = "FINAL" if is_final else "interim"
            logger.debug(
                f"Published {result_type} result to {channel}: {message['text'][:50]}..."
            )

        except Exception as e:
            logger.error(
                f"Error publishing to client for {session_id}: {e}", exc_info=True
            )

    async def store_final_result(
        self, session_id: str, result: Dict, chunk_id: str = None
    ):
        """
        Store final transcription result to Redis Stream.

        Args:
            session_id: Session ID
            result: Final transcription result
            chunk_id: Optional chunk identifier
        """
        try:
            stream_name = f"transcription:results:{session_id}"

            # Get words and segments directly
            words = result.get("words", [])
            segments = result.get("segments", [])

            # Prepare result entry
            entry = {
                b"text": result.get("text", "").encode(),
                b"chunk_id": (chunk_id or f"final_{int(time.time() * 1000)}").encode(),
                b"provider": b"streaming",
                b"confidence": str(result.get("confidence", 0.0)).encode(),
                b"processing_time": b"0.0",
                b"timestamp": str(time.time()).encode(),
            }

            if words:
                entry[b"words"] = json.dumps(words).encode()

            if segments:
                entry[b"segments"] = json.dumps(segments).encode()

            # Write to Redis Stream
            await self.redis_client.xadd(stream_name, entry)

            logger.info(
                f"Stored final result to {stream_name}: {result.get('text', '')[:50]}... ({len(words)} words)"
            )

        except Exception as e:
            logger.error(
                f"Error storing final result for {session_id}: {e}", exc_info=True
            )

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

    async def trigger_plugins(
        self, session_id: str, result: Dict, speaker_name: Optional[str] = None
    ):
        """
        Trigger plugins at streaming_transcript access level (final results only).

        Checks primary speaker gating before dispatching:
        - If user has primary_speakers configured AND a speaker was identified,
          only dispatch if the speaker is in the primary speakers list.
        - If speaker identification is unavailable, plugins still fire (no blocking).

        Args:
            session_id: Session ID (client_id from stream name)
            result: Final transcription result
            speaker_name: Identified speaker name (or None if unavailable)
        """
        try:
            # Find user_id by looking up session with matching client_id
            user_id = await self._get_user_id_from_client_id(session_id)

            if not user_id:
                logger.warning(
                    f"Could not find user_id for client_id {session_id}. "
                    "Plugins will not be triggered."
                )
                return

            # Primary speaker gating
            if speaker_name:
                try:
                    user = await get_user_by_id(user_id)
                    if user and user.primary_speakers:
                        primary_speaker_names = {
                            ps["name"].strip().lower() for ps in user.primary_speakers
                        }
                        if speaker_name.strip().lower() not in primary_speaker_names:
                            logger.info(
                                f"Skipping plugins - speaker '{speaker_name}' "
                                f"not a primary speaker for user {user_id}"
                            )
                            return
                except Exception as e:
                    logger.warning(f"Error checking primary speakers: {e}")
                    # Don't block plugins on lookup failure

            plugin_data = {
                "transcript": result.get("text", ""),
                "session_id": session_id,
                "words": result.get("words", []),
                "segments": result.get("segments", []),
                "confidence": result.get("confidence", 0.0),
                "is_final": True,
            }

            # Include speaker info if available
            if speaker_name:
                plugin_data["speaker_name"] = speaker_name

            # Dispatch transcript.streaming event
            logger.info(
                f"Dispatching transcript.streaming event for user {user_id}, "
                f"speaker={speaker_name}, transcript: {plugin_data['transcript'][:50]}..."
            )

            plugin_results = await self.plugin_router.dispatch_event(
                event=PluginEvent.TRANSCRIPT_STREAMING,
                user_id=user_id,
                data=plugin_data,
                metadata={"client_id": session_id},
            )

            if plugin_results:
                logger.info(
                    f"Plugins triggered successfully: {len(plugin_results)} results"
                )
            else:
                logger.info(f"No plugins triggered (no matching conditions)")

        except Exception as e:
            logger.error(
                f"Error triggering plugins for {session_id}: {e}", exc_info=True
            )

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
            "started_at": time.time(),
        }

        # Read actual sample rate from the session's audio_format stored in Redis
        sample_rate = 16000
        session_key = f"audio:session:{session_id}"
        try:
            audio_format_raw = await self.redis_client.hget(session_key, "audio_format")
            if audio_format_raw:
                audio_format = json.loads(audio_format_raw)
                sample_rate = int(audio_format.get("rate", 16000))
                logger.info(
                    f"Read sample rate {sample_rate}Hz from session {session_id}"
                )
        except Exception as e:
            logger.warning(
                f"Failed to read audio_format from Redis for {session_id}: {e}"
            )

        # Start WebSocket connection to transcription provider
        await self.start_session_stream(session_id, sample_rate=sample_rate)

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
                        block=1000,  # Block for 1 second
                    )

                    if not messages:
                        # No new messages - check if stream is still alive
                        if session_id not in self.active_sessions:
                            logger.info(
                                f"Session {session_id} no longer active, ending stream processing"
                            )
                            stream_ended = True
                        continue

                    for stream, stream_messages in messages:
                        logger.debug(
                            f"Read {len(stream_messages)} messages from {stream_name}"
                        )
                        for message_id, fields in stream_messages:
                            msg_id = (
                                message_id.decode()
                                if isinstance(message_id, bytes)
                                else message_id
                            )

                            # Check for end marker
                            if fields.get(b"end_marker") or fields.get("end_marker"):
                                logger.info(f"End marker received for {session_id}")
                                stream_ended = True
                                # ACK the end marker
                                await self.redis_client.xack(
                                    stream_name, self.group_name, msg_id
                                )
                                break

                            # Extract audio data (producer sends as 'audio_data', not 'audio_chunk')
                            audio_chunk = fields.get(b"audio_data") or fields.get(
                                "audio_data"
                            )
                            if audio_chunk:
                                logger.debug(
                                    f"Processing audio chunk {msg_id} ({len(audio_chunk)} bytes)"
                                )
                                # Process audio chunk through streaming provider
                                await self.process_audio_chunk(
                                    session_id=session_id,
                                    audio_chunk=audio_chunk,
                                    chunk_id=msg_id,
                                )
                            else:
                                logger.warning(
                                    f"Message {msg_id} has no audio_data field"
                                )

                            # ACK the message after processing
                            await self.redis_client.xack(
                                stream_name, self.group_name, msg_id
                            )

                        if stream_ended:
                            break

                except redis_exceptions.ResponseError as e:
                    if "NOGROUP" in str(e):
                        # Stream has expired or been deleted - exit gracefully
                        logger.info(
                            f"Stream {stream_name} expired or deleted, ending processing"
                        )
                        stream_ended = True
                        break
                    else:
                        logger.error(
                            f"Redis error reading from stream {stream_name}: {e}",
                            exc_info=True,
                        )
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.error(
                        f"Error reading from stream {stream_name}: {e}", exc_info=True
                    )
                    await asyncio.sleep(1)

        finally:
            # End WebSocket connection
            await self.end_session_stream(session_id)

            # Remove from active streams tracking
            self.active_streams.pop(stream_name, None)
            logger.debug(f"Removed {stream_name} from active streams tracking")

            # Attempt to delete the stream if all consumer groups have finished processing.
            # This prevents the discovery loop from re-discovering the stream during the
            # TTL window (set by cleanup_client_state) and spawning zombie process_stream tasks.
            try:
                await self._try_delete_finished_stream(stream_name)
            except Exception as e:
                logger.debug(
                    f"Stream cleanup check failed for {stream_name} (non-fatal): {e}"
                )

    async def _try_delete_finished_stream(self, stream_name: str):
        """
        Delete a Redis stream if all consumer groups have finished processing.

        Both consumer groups (streaming-transcription and audio_persistence) read from
        the same stream. We only delete when both have 0 pending messages to avoid
        breaking the other consumer. If any group still has pending messages or not all
        expected groups are registered, the 60s TTL fallback handles cleanup.
        """
        _EXPECTED_GROUPS = {"streaming-transcription", "audio_persistence"}

        if not await self.redis_client.exists(stream_name):
            return

        groups = await self.redis_client.execute_command("XINFO", "GROUPS", stream_name)
        if not groups:
            return

        # Parse all groups — XINFO GROUPS returns a flat key-value array per group
        registered_names = set()
        total_pending = 0
        for group in groups:
            group_dict = {}
            for i in range(0, len(group), 2):
                key = (
                    group[i].decode() if isinstance(group[i], bytes) else str(group[i])
                )
                value = group[i + 1]
                if isinstance(value, bytes):
                    try:
                        value = value.decode()
                    except UnicodeDecodeError:
                        value = str(value)
                group_dict[key] = value

            name = group_dict.get("name", "")
            pending = int(group_dict.get("pending", 0))
            registered_names.add(name)
            total_pending += pending

        if not _EXPECTED_GROUPS.issubset(registered_names):
            logger.debug(
                f"Stream {stream_name}: not all consumer groups registered yet "
                f"(found: {registered_names}), skipping delete"
            )
            return

        if total_pending > 0:
            logger.debug(
                f"Stream {stream_name} still has {total_pending} pending messages "
                f"across consumer groups, skipping delete"
            )
            return

        # All expected groups registered, all have 0 pending — safe to delete
        await self.redis_client.delete(stream_name)
        logger.info(
            f"Deleted stream {stream_name} "
            f"(all {len(_EXPECTED_GROUPS)} consumer groups have 0 pending)"
        )

    async def start_consuming(self):
        """
        Start consuming audio streams and processing through streaming transcription.
        Uses Redis consumer groups for fan-out (allows batch workers to process same stream).
        """
        self.running = True
        logger.info(f"Streaming consumer started (group: {self.group_name})")

        try:
            while self.running:
                # Discover available streams
                streams = await self.discover_streams()

                if streams:
                    logger.debug(f"Discovered {len(streams)} audio streams")
                else:
                    logger.debug("No audio streams found")

                # Setup consumer groups and spawn processing tasks
                for stream_name in streams:
                    if stream_name in self.active_streams:
                        continue  # Already processing

                    # Check if this stream was already fully processed.
                    # end_session_stream sets transcription:complete:{session_id} with 5-min TTL.
                    # Without this check, re-discovered streams spawn zombie tasks that each
                    # open a new transcription provider connection, exhausting connection limits.
                    session_id = stream_name.replace("audio:stream:", "")
                    completion_key = f"transcription:complete:{session_id}"
                    if await self.redis_client.exists(completion_key):
                        logger.debug(
                            f"Stream {stream_name} already completed, skipping"
                        )
                        continue

                    # Setup consumer group (no manual lock needed)
                    await self.setup_consumer_group(stream_name)

                    # Track stream and spawn task to process it
                    self.active_streams[stream_name] = {"session_id": session_id}

                    # Spawn task to process this stream
                    asyncio.create_task(self.process_stream(stream_name))
                    logger.info(
                        f"Now consuming from {stream_name} (group: {self.group_name})"
                    )

                # Sleep before next discovery cycle (1s for fast discovery)
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Fatal error in consumer main loop: {e}", exc_info=True)
        finally:
            await self.stop()

    async def stop(self):
        """Stop consuming and clean up resources."""
        logger.info("Stopping streaming consumer...")
        self.running = False

        # End all active sessions
        session_ids = list(self.active_sessions.keys())
        for session_id in session_ids:
            try:
                await self.end_session_stream(session_id)
            except Exception as e:
                logger.error(f"Error ending session {session_id}: {e}")

        logger.info("Streaming consumer stopped")
