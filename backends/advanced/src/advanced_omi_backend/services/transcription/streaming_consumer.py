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
from websockets.exceptions import ConnectionClosed

from advanced_omi_backend.heartbeat import beat
from advanced_omi_backend.models.user import get_user_by_id
from advanced_omi_backend.observability.otel_setup import set_span_attrs
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.plugins.router import PluginRouter
from advanced_omi_backend.redis_keys import (
    SessionId,
    parse_audio_stream_name,
    transcription_results_stream,
)
from advanced_omi_backend.services.audio_stream.durability import (
    AUDIO_PERSISTENCE_GROUP,
    delete_stream_if_durable,
    session_append_closed,
)
from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
)
from advanced_omi_backend.services.transcription import get_transcription_provider
from advanced_omi_backend.services.wakeword.followup import maybe_handle_followup
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
from advanced_omi_backend.utils.audio_utils import pcm_to_wav_bytes

logger = logging.getLogger(__name__)

MAX_STREAMING_START_ATTEMPTS = 2

# Attempts when re-establishing a provider stream after a mid-session connection
# drop (e.g. the provider enforces a max WebSocket session duration). More patient
# than initial start: audio chunks queue in Redis while we retry, so a successful
# reconnect loses nothing.
STREAMING_RECONNECT_ATTEMPTS = 5

# Bail out of process_stream after this many seconds with no incoming chunks.
# process_stream's normal exit is the end_marker sent by AudioStreamProducer.finalize_session.
# That marker is missed when:
#   - the device drops the TCP connection without a clean WebSocket close (no FastAPI
#     disconnect handler fires, so finalize_session never runs)
#   - the backend process crashes / is restarted before finalize_session runs
# Without a heartbeat exit the task pins on the stream forever, polling XREADGROUP every
# ~1s. Streams idle 65+ days have been observed in prod. Threshold is generous enough to
# ride out brief network blips (producer emits chunks every 0.25s when healthy).
STREAM_IDLE_TIMEOUT_SECONDS = 300

# How recently a still-ACTIVE session must have appended for its stream to count as
# resumed rather than merely quiet. A healthy producer emits a chunk every 0.25s.
STREAM_RESUME_MAX_AGE_SECONDS = 10.0

# Entries read from the tail when probing for the producer's end marker. The marker is
# the last thing finalize_session appends, so 1 would normally do; a small window keeps
# the probe correct if a chunk raced in behind it.
STREAM_TAIL_PROBE_ENTRIES = 5


def _is_connection_error(e: Exception) -> bool:
    """Check if exception indicates WebSocket connection death."""
    if isinstance(e, (ConnectionClosed, ConnectionError, OSError)):
        return True
    # Check wrapped exceptions
    cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
    if cause and isinstance(cause, (ConnectionClosed, ConnectionError, OSError)):
        return True
    return False


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


def _apply_time_offset(result: dict, offset: float) -> None:
    """Shift all word/segment timestamps in ``result`` by ``offset`` seconds, in-place.

    Streaming providers stamp words relative to their own WebSocket session, not
    the audio session. When a provider stream is restarted mid-session its clock
    resets to 0; without re-offsetting, late audio gets timestamps that collide
    with the start of the conversation and downstream segment-building interleaves
    the two timelines (words are bucketed into diarized segments by time).
    """
    if not offset:
        return
    for w in result.get("words") or []:
        if not isinstance(w, dict):
            continue
        if w.get("start") is not None:
            w["start"] = w["start"] + offset
        if w.get("end") is not None:
            w["end"] = w["end"] + offset
    for s in result.get("segments") or []:
        if not isinstance(s, dict):
            continue
        if s.get("start") is not None:
            s["start"] = s["start"] + offset
        if s.get("end") is not None:
            s["end"] = s["end"] + offset
        for w in s.get("words") or []:
            if not isinstance(w, dict):
                continue
            if w.get("start") is not None:
                w["start"] = w["start"] + offset
            if w.get("end") is not None:
                w["end"] = w["end"] + offset


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
        self.store = SessionStore(redis_client)
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

        # Strong refs to spawned per-stream tasks so they aren't GC'd mid-flight,
        # and so a crash surfaces (via the done-callback) instead of being a
        # swallowed "Task exception was never retrieved".
        self._stream_tasks: set[asyncio.Task] = set()

        # Session tracking for WebSocket connections
        self.active_sessions: Dict[str, Dict] = (
            {}
        )  # {session_id: {"last_activity": timestamp}}

        # Audio buffers for speaker identification (raw PCM bytes per session)
        self._audio_buffers: Dict[str, bytearray] = {}

        # Cumulative audio seconds sent to the provider per session, across
        # provider stream restarts. This is the session-relative clock used to
        # re-offset provider timestamps after a mid-session reconnect (the
        # provider clock restarts at 0 on every new WebSocket session).
        self._session_audio_seconds: Dict[str, float] = {}

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

    async def _session_resumed(self, stream_name: str, session_id: str) -> bool:
        """True if a stream that carries a completion flag is still being written to.

        Answering this wrong in the permissive direction is expensive: clearing the
        flag revokes the handshake ``open_conversation_job`` is blocked on, and no
        replacement signal ever arrives, so the conversation stalls for that job's
        full 30s wait before finishing without it.

        Recency alone cannot answer it. ``finalize_session`` flushes the residual
        audio and appends the end marker as its *last* act, so at the exact moment
        the flag is set the newest entry is milliseconds old — a closing session is
        indistinguishable from a resuming one by age. Two causal facts decide it
        instead:

        - **Session status.** ``producer._append_owned_message`` appends inside a
          WATCH/MULTI whose precondition is ``status == "active"``, so a session that
          has left ACTIVE can never receive another entry. Its stream is frozen, and
          whatever sits at the tail is its own closing flush.
        - **The end marker.** It is appended (while still ACTIVE) strictly before the
          consumer can read it and set the flag, so its presence proves the producer
          finished even if the FINALIZING status write has not landed yet.

        Only when neither says "finished" does recency get to speak, and there it
        answers the question it is actually good at: whether audio is flowing now, or
        the consumer gave up on a stream that has been silent for a long time.

        Errors return False — declining to re-attach costs a resumed session its
        streaming transcription, but wrongly re-attaching corrupts the handshake for
        every conversation on the session.
        """
        try:
            if await self.store.get_status(session_id) != SessionStatus.ACTIVE:
                return False

            entries = await self.redis_client.xrevrange(
                stream_name, count=STREAM_TAIL_PROBE_ENTRIES
            )
            if not entries:
                return False
            if any(
                fields.get(b"end_marker") or fields.get("end_marker")
                for _, fields in entries
            ):
                return False

            # Redis stream ids are ``<ms>-<seq>``, so age comes free from the id.
            entry_id = entries[0][0]
            if isinstance(entry_id, bytes):
                entry_id = entry_id.decode()
            entry_ms = int(entry_id.split("-")[0])
            return (time.time() * 1000 - entry_ms) < (
                STREAM_RESUME_MAX_AGE_SECONDS * 1000
            )
        except Exception as e:  # noqa: BLE001 — best-effort liveness probe
            logger.debug(f"Resume probe failed for {stream_name}: {e}")
            return False

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

    async def start_session_stream(
        self,
        session_id: str,
        sample_rate: int = 16000,
        attempts: int = MAX_STREAMING_START_ATTEMPTS,
    ):
        """
        Start WebSocket connection to transcription provider for a session.

        Resumes the session-relative clock: the provider stamps words relative to
        its own WebSocket session, so on a mid-session restart we record the audio
        seconds already transcribed as ``time_offset`` and shift all subsequent
        results by it (see _apply_time_offset).

        Args:
            session_id: Session ID (client_id from audio stream)
            sample_rate: Audio sample rate in Hz
            attempts: Connection attempts before giving up (5s apart)
        """
        # Audio seconds already sent in prior provider sessions of this audio
        # session — 0.0 for a fresh session. Falls back to the session hash so
        # the clock also survives a consumer process restart.
        time_offset = self._session_audio_seconds.get(session_id)
        if time_offset is None:
            time_offset = await self.store.get_transcription_seconds(session_id)
            self._session_audio_seconds[session_id] = time_offset

        last_error = None
        for attempt in range(attempts):
            try:
                await self.provider.start_stream(
                    stream_id=session_id,
                    sample_rate=sample_rate,
                    diarize=self._provider_has_diarization,
                )

                self.active_sessions[session_id] = {
                    "last_activity": time.time(),
                    "sample_rate": sample_rate,
                    "time_offset": time_offset,
                    "last_health_persisted_at": 0.0,
                }
                await self.store.mark_transcription_provider_connected(session_id)

                # Only buffer audio for speaker identification when provider lacks diarization
                if not self._provider_has_diarization:
                    self._audio_buffers[session_id] = bytearray()

                logger.info(
                    f"Started streaming transcription for session: {session_id}"
                    + (f" (resuming at +{time_offset:.1f}s)" if time_offset else "")
                )
                return

            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    logger.warning(
                        f"Failed to start stream for {session_id} "
                        f"(attempt {attempt + 1}/{attempts}): {e}. "
                        f"Retrying in 5s..."
                    )
                    await asyncio.sleep(5)
                else:
                    logger.error(
                        f"Failed to start stream for {session_id} "
                        f"(attempt {attempt + 1}/{attempts}): {e}",
                        exc_info=True,
                    )

        # All attempts failed — set error flag and raise
        try:
            await self.store.set_transcription_error(session_id, str(last_error))
            logger.info(f"Set transcription error flag for {session_id}")
        except Exception as redis_error:
            logger.warning(f"Failed to set error flag in Redis: {redis_error}")

        raise last_error

    async def _reconnect_session(self, session_id: str) -> bool:
        """Re-establish the provider stream after a mid-session connection drop.

        Persists the audio-seconds counter (so the clock survives even a consumer
        restart), tears down the dead provider-side state, and opens a fresh
        provider stream whose results are shifted by the recorded offset.

        Returns True when a new provider stream is live; False when all attempts
        failed (start_session_stream has then already set the transcription_error
        flag — terminal for this session).
        """
        seconds_sent = self._session_audio_seconds.get(session_id, 0.0)
        try:
            await self.store.set_transcription_seconds(session_id, seconds_sent)
        except Exception as e:
            logger.warning(
                f"Failed to persist transcription clock for {session_id}: {e}"
            )

        # Tear down dead provider-side state; the socket is already gone so
        # errors here are expected and ignored.
        try:
            await self.provider.end_stream(stream_id=session_id)
        except Exception:
            pass

        sample_rate = (self.active_sessions.get(session_id) or {}).get(
            "sample_rate", 16000
        )
        try:
            await self.start_session_stream(
                session_id,
                sample_rate=sample_rate,
                attempts=STREAMING_RECONNECT_ATTEMPTS,
            )
            return True
        except Exception as e:
            logger.error(
                f"Could not reconnect streaming transcription for {session_id}: {e}"
            )
            return False

    async def end_session_stream(self, session_id: str):
        """
        End WebSocket connection to transcription provider for a session.

        Args:
            session_id: Session ID
        """
        completion_status = "1"
        try:
            # Get final result from provider
            final_result = await self.provider.end_stream(stream_id=session_id)

            # If there's a final result, publish it
            if final_result and final_result.get("text"):
                words = final_result.get("words") or []
                _normalize_words(words)
                _apply_time_offset(
                    final_result,
                    (self.active_sessions.get(session_id) or {}).get(
                        "time_offset", 0.0
                    ),
                )

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

            logger.info(
                f"Streaming transcription complete for {session_id} (signal set)"
            )

        except Exception as e:
            logger.error(f"Error ending stream for {session_id}: {e}", exc_info=True)
            # Still signal completion even on error so conversation job doesn't hang
            completion_status = "error"

        finally:
            try:
                await self.store.mark_transcription_provider_disconnected(session_id)
            except Exception:
                logger.warning(
                    f"Failed to mark transcription provider disconnected for {session_id}"
                )
            # Cleanup must run on both paths (previously the error path leaked
            # active_sessions / audio buffer entries).
            self.active_sessions.pop(session_id, None)
            self._audio_buffers.pop(session_id, None)

            # Persist the session-relative audio clock so a later re-discovery of
            # a still-live stream resumes timestamps instead of restarting at 0.
            seconds_sent = self._session_audio_seconds.pop(session_id, None)
            if seconds_sent:
                try:
                    await self.store.set_transcription_seconds(session_id, seconds_sent)
                except Exception:
                    pass  # Best effort

            # Signal that streaming transcription is complete for this session
            try:
                completion_key = f"transcription:complete:{session_id}"
                await self.redis_client.set(completion_key, completion_status, ex=300)
                if completion_status == "error":
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
                stream_id=session_id, audio_chunk=audio_chunk
            )

            # Update last activity and advance the session-relative audio clock
            # (pipeline audio is PCM 16-bit mono). Counted only after a successful
            # send — a chunk that dies mid-send is re-sent after reconnect.
            session = self.active_sessions.get(session_id)
            if session is not None:
                now = time.time()
                session["last_activity"] = now
                self._session_audio_seconds[session_id] = (
                    self._session_audio_seconds.get(session_id, 0.0)
                    + len(audio_chunk) / (session.get("sample_rate", 16000) * 2)
                )
                # Audio chunks can arrive several times per second. Persisting
                # this health timestamp at most every five seconds keeps the
                # cross-worker signal useful without amplifying Redis traffic.
                if now - session.get("last_health_persisted_at", 0.0) >= 5.0:
                    await self.store.mark_transcription_audio_sent(session_id)
                    session["last_health_persisted_at"] = now

            # Provider returns None if no response yet, or a dict with results
            if result:
                await self.store.mark_transcription_provider_message(session_id)
                is_final = result.get("is_final", False)
                text = result.get("text", "")
                words = result.get("words") or []
                word_count = len(words)

                # Normalize provider-specific word field names (e.g. start_time → start)
                _normalize_words(words)

                # Provider timestamps are relative to its own WebSocket session;
                # shift by the audio already transcribed in prior provider
                # sessions so timestamps stay session-relative across reconnects.
                _apply_time_offset(result, (session or {}).get("time_offset", 0.0))

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
            if _is_connection_error(e):
                # Recoverable: process_stream attempts an in-place reconnect.
                # The transcription_error flag (which terminates speech detection)
                # is only set if the reconnect ultimately fails
                # (start_session_stream sets it after exhausting attempts).
                logger.error(f"Transcription connection lost for {session_id}: {e}")
                raise  # Let process_stream handle reconnect/terminate
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
            identity = await self._resolve_session_identity(session_id)
            if identity is None:
                return None, 0.0
            user_id, _client_id = identity

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
                "words": result.get("words") or [],
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
        self, session_id: str, result: Dict, chunk_id: Optional[str] = None
    ):
        """
        Store final transcription result to Redis Stream.

        Args:
            session_id: Session ID
            result: Final transcription result
            chunk_id: Optional chunk identifier
        """
        set_span_attrs(pipeline_stage="transcription_streaming")
        try:
            session_ref = SessionId.from_value(session_id, "session_id")
            stream_name = str(transcription_results_stream(session_ref))

            # Get words and segments directly
            words = result.get("words") or []
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

    async def _resolve_session_identity(
        self, session_id: str
    ) -> Optional[tuple[str, str]]:
        """
        Resolve the user and device identity attached to an audio session.

        ``session_id`` and ``client_id`` are different identities. The session hash
        is the authoritative join point between them; using one as a fallback for
        the other reintroduces the class of routing bug this consumer must prevent.
        """
        view = await self.store.read(session_id)
        if view is None:
            logger.warning(
                f"No audio session metadata found for session_id {session_id}. "
                "Dependent processing will not run."
            )
            return None
        if not view.user_id:
            logger.warning(
                f"Audio session {session_id} has no user_id. "
                "Dependent processing will not run."
            )
            return None
        if not view.client_id:
            logger.warning(
                f"Audio session {session_id} has no client_id. "
                "Dependent processing will not run."
            )
            return None
        return view.user_id, view.client_id

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
            session_id: Audio session ID from the Redis stream name
            result: Final transcription result
            speaker_name: Identified speaker name (or None if unavailable)
        """
        try:
            identity = await self._resolve_session_identity(session_id)
            if identity is None:
                return
            user_id, client_id = identity

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

            # Wake-word follow-up: if a follow-up window is open for this session,
            # treat this utterance as a contextual follow-up (no wake word needed)
            # and stop — don't run normal transcript dispatch for it.
            try:
                handled = await maybe_handle_followup(
                    self.redis_client,
                    self.plugin_router,
                    user_id=user_id,
                    session_id=session_id,
                    client_id=client_id,
                    text=result.get("text", ""),
                )
                if handled:
                    return
            except (
                Exception
            ) as e:  # noqa: BLE001 - never let follow-up break transcription
                logger.error(f"Follow-up handling error: {e}", exc_info=True)

            plugin_data = {
                "transcript": result.get("text", ""),
                "session_id": session_id,
                "client_id": client_id,
                "words": result.get("words") or [],
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
                metadata={"client_id": client_id, "session_id": session_id},
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
        session_id = str(parse_audio_stream_name(stream_name))

        # Track this stream
        self.active_streams[stream_name] = {
            "session_id": session_id,
            "started_at": time.time(),
        }

        # Read actual sample rate from the session's audio_format stored in Redis
        sample_rate, _, _ = await self.store.get_audio_format(session_id)
        logger.info(f"Read sample rate {sample_rate}Hz from session {session_id}")

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
                        if session_id not in self.active_sessions:
                            logger.info(
                                f"Session {session_id} no longer active, ending stream processing"
                            )
                            stream_ended = True
                            continue

                        # Heartbeat-based zombie exit (see STREAM_IDLE_TIMEOUT_SECONDS).
                        idle_for = (
                            time.time()
                            - self.active_sessions[session_id]["last_activity"]
                        )
                        if idle_for > STREAM_IDLE_TIMEOUT_SECONDS:
                            logger.warning(
                                f"Stream {stream_name} idle for {idle_for:.0f}s without "
                                f"end_marker — treating as zombie and ending processing"
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
                                if session_id in self.active_sessions:
                                    self.active_sessions[session_id][
                                        "last_activity"
                                    ] = time.time()
                                # Process audio chunk through streaming provider
                                try:
                                    await self.process_audio_chunk(
                                        session_id=session_id,
                                        audio_chunk=audio_chunk,
                                        chunk_id=msg_id,
                                    )
                                except Exception as e:
                                    if _is_connection_error(e):
                                        logger.warning(
                                            f"Connection lost for {session_id} — "
                                            f"attempting in-place reconnect"
                                        )
                                        if await self._reconnect_session(session_id):
                                            # Re-send the chunk that died mid-flight
                                            # to the new provider stream.
                                            try:
                                                await self.process_audio_chunk(
                                                    session_id=session_id,
                                                    audio_chunk=audio_chunk,
                                                    chunk_id=msg_id,
                                                )
                                            except Exception as resend_err:
                                                logger.error(
                                                    f"Re-send of chunk {msg_id} after "
                                                    f"reconnect failed for {session_id}: "
                                                    f"{resend_err} — dropping chunk"
                                                )
                                            # Reconnected: fall through to ACK
                                        else:
                                            logger.error(
                                                f"Reconnect failed — stopping stream "
                                                f"{session_id}"
                                            )
                                            stream_ended = True
                                            break  # Don't ACK — leave chunks pending
                                    # Non-connection error: fall through to ACK
                            else:
                                logger.warning(
                                    f"Message {msg_id} has no audio_data field"
                                )

                            # ACK only on success or non-fatal error
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
        Delete only after Redis proves every registered group has no pending or lag.

        This consumer and Mongo persistence are required. Optional groups such as
        wakeword_detection also block deletion when present. There is deliberately no
        age/TTL fallback: inability to prove durability means retaining the stream.
        """
        session_id = str(parse_audio_stream_name(stream_name))
        if not await session_append_closed(self.redis_client, session_id):
            logger.debug(
                f"Retaining stream {stream_name}: session may still append to it"
            )
            return

        decision = await delete_stream_if_durable(
            self.redis_client,
            stream_name,
            required_groups={self.group_name, AUDIO_PERSISTENCE_GROUP},
        )
        if decision.safe_to_delete:
            logger.info(
                f"Deleted stream {stream_name} after durability proof "
                f"({len(decision.groups)} groups drained)"
            )
        else:
            logger.debug(f"Retaining stream {stream_name}: {decision.reason}")

    def _on_stream_task_done(self, stream_name: str, task: asyncio.Task) -> None:
        """Surface a finished per-stream task and free a crashed stream.

        process_stream pops active_streams in its own finally on the normal path
        (so the pop here is a no-op then). But if it crashed BEFORE that finally
        — e.g. get_audio_format / start_session_stream raised — the stream would
        stay marked active and never be re-picked, silently dropping that
        session. Log the crash loudly and drop the stream so discovery retries it.
        """
        self._stream_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Stream task for {stream_name} crashed: {exc}", exc_info=exc)
            self.active_streams.pop(stream_name, None)

    async def start_consuming(self, heartbeat_name: str | None = None):
        """
        Start consuming audio streams and processing through streaming transcription.
        Uses Redis consumer groups for fan-out (allows batch workers to process same stream).

        Args:
            heartbeat_name: If set, beat ``worker:heartbeat:{name}`` once per
                discovery iteration so the workers healthcheck can tell this
                consumer's main loop is still turning (not wedged-but-alive).
        """
        self.running = True
        logger.info(f"Streaming consumer started (group: {self.group_name})")

        try:
            while self.running:
                if heartbeat_name:
                    await beat(self.redis_client, heartbeat_name)

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
                    session_id = str(parse_audio_stream_name(stream_name))
                    completion_key = f"transcription:complete:{session_id}"
                    if await self.redis_client.exists(completion_key):
                        # The flag can outlive the provider stream it describes: a
                        # process_stream task that exits on its idle heartbeat sets it
                        # while the session is still ACTIVE, and the device may resume
                        # sending afterwards. Discovery would then skip that live stream
                        # until the 5-min TTL, starving it of transcription.
                        #
                        # Self-heal, but only for a session that can still produce. The
                        # flag is also the handshake open_conversation_job waits on, so
                        # clearing it for a session that has finished stalls that job for
                        # its full 30s wait (see _session_resumed).
                        if await self._session_resumed(stream_name, session_id):
                            logger.info(
                                f"Stream {stream_name} marked complete but its session "
                                f"is still active and producing — clearing flag and "
                                f"re-attaching"
                            )
                            await self.redis_client.delete(completion_key)
                        else:
                            logger.debug(
                                f"Stream {stream_name} already completed, skipping"
                            )
                            continue

                    # Setup consumer group (no manual lock needed)
                    await self.setup_consumer_group(stream_name)

                    # Track stream and spawn task to process it
                    self.active_streams[stream_name] = {"session_id": session_id}

                    # Spawn task to process this stream. Keep a strong ref and a
                    # done-callback so a crash before process_stream's own
                    # try/finally (e.g. provider connect failure) is logged and the
                    # stream is freed for re-discovery instead of silently stuck.
                    task = asyncio.create_task(self.process_stream(stream_name))
                    self._stream_tasks.add(task)
                    task.add_done_callback(
                        lambda t, sn=stream_name: self._on_stream_task_done(sn, t)
                    )
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
