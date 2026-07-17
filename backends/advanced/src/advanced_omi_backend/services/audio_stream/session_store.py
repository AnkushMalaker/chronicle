"""Typed accessor for per-session state stored in the Redis hash ``audio:session:{id}``.

The session hash is the cross-process single source of truth for a streaming
session (the FastAPI process, the streaming consumer, and the RQ workers all read
and write it). This module is the one place that owns:

- the key format (``audio:session:{id}``, ``session:signal:{id}``,
  ``session:conversation_count:{id}``),
- the field schema (see :class:`SessionView`),
- encode/decode of stringly-typed values (bytes-or-str, "true"/"false", ints,
  JSON blobs, enums),
- the named lifecycle operations (init / finalize / complete / close-request).

``SessionStore`` wraps an *existing* async redis client — it never constructs its
own — because several clients with different lifecycles are in play (the producer
singleton, the ``@async_job``-injected worker client, the streaming consumer's
client). Reads tolerate both bytes and str values since clients differ in their
``decode_responses`` setting.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import AsyncIterator, Literal, Optional

logger = logging.getLogger(__name__)

# Key templates
_SESSION_KEY = "audio:session:{}"
_SIGNAL_CHANNEL = "session:signal:{}"
_CONV_COUNT_KEY = "session:conversation_count:{}"
_CONV_COUNT_TTL = 3600

# Default audio format: 16 kHz / mono / 16-bit
_DEFAULT_AUDIO_FORMAT = (16000, 1, 2)

CompletionReason = Literal[
    "websocket_disconnect",
    "user_stopped",
    "inactivity_timeout",
    "max_duration",
    "all_jobs_complete",
]


class SessionStatus(str, Enum):
    """Lifecycle status of a session, stored in the ``status`` field."""

    ACTIVE = "active"
    FINALIZING = "finalizing"
    FINISHED = "finished"


class SpeakerCheckStatus(str, Enum):
    """Result of the speaker-enrollment check, stored in ``speaker_check_status``."""

    CHECKING = "checking"
    ENROLLED = "enrolled"
    NOT_ENROLLED = "not_enrolled"
    FAILED = "failed"
    TIMEOUT = "timeout"


def _to_str(value) -> Optional[str]:
    """Normalize a Redis value (bytes or str or None) to str or None."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode()
    return value


def _coerce_enum(enum_cls, value: Optional[str]):
    """Tolerant enum coercion: unknown/empty values become None rather than raising."""
    if value in (None, ""):
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SessionView:
    """Typed, decoded snapshot of an ``audio:session:{id}`` hash.

    Every field declared here IS the session schema. Missing keys fall back to the
    declared defaults, so reads never raise on a partially-populated hash.
    """

    session_id: str
    # identity / config
    user_id: str = ""
    user_email: str = ""
    client_id: str = ""
    connection_id: str = ""
    stream_name: str = ""
    provider: str = ""
    mode: str = ""
    # timestamps (unix float; speech_detected_at is kept as ISO str)
    started_at: float = 0.0
    last_chunk_at: float = 0.0
    finalized_at: Optional[float] = None
    completed_at: Optional[float] = None
    speech_detected_at: str = ""
    # counters
    chunks_published: int = 0
    # audio seconds sent to the streaming transcription provider (session-relative
    # clock; persisted so word-timestamp offsets survive provider reconnects)
    transcription_seconds_sent: float = 0.0
    transcription_provider_status: str = ""
    transcription_provider_connected_at: float = 0.0
    transcription_last_audio_sent_at: float = 0.0
    transcription_last_message_at: float = 0.0
    # job ids
    speech_detection_job_id: str = ""
    audio_persistence_job_id: str = ""
    # bool-as-string
    websocket_connected: bool = False
    # enums
    status: Optional[SessionStatus] = None
    speaker_check_status: Optional[SpeakerCheckStatus] = None
    # JSON blobs
    audio_format: Optional[dict] = None
    markers: list = field(default_factory=list)
    # free strings
    completion_reason: str = ""
    conversation_close_requested: str = ""
    transcription_error: str = ""
    last_event: str = ""
    identified_speakers: list = field(default_factory=list)

    @property
    def audio_format_tuple(self) -> tuple[int, int, int]:
        """``(sample_rate, channels, sample_width)`` with 16 kHz/mono/16-bit fallback."""
        fmt = self.audio_format or {}
        try:
            return (
                int(fmt.get("rate", 16000)),
                int(fmt.get("channels", 1)),
                int(fmt.get("width", 2)),
            )
        except (TypeError, ValueError):
            return _DEFAULT_AUDIO_FORMAT

    @classmethod
    def from_hash(cls, session_id: str, raw: dict) -> "SessionView":
        """Build a view from a raw Redis hash (keys/values may be bytes or str)."""
        d = {_to_str(k): _to_str(v) for k, v in raw.items()}

        def s(key: str, default: str = "") -> str:
            v = d.get(key)
            return v if v not in (None, "") else default

        def ffloat(key: str) -> Optional[float]:
            v = d.get(key)
            if v in (None, ""):
                return None
            try:
                return float(v)
            except ValueError:
                return None

        def fint(key: str, default: int = 0) -> int:
            v = d.get(key)
            if v in (None, ""):
                return default
            try:
                return int(v)
            except ValueError:
                return default

        def fjson(key: str, default):
            v = d.get(key)
            if v in (None, ""):
                return default
            try:
                return json.loads(v)
            except (ValueError, TypeError):
                logger.warning(
                    f"Malformed JSON in session {session_id} field '{key}': {v!r}"
                )
                return default

        speakers_raw = s("identified_speakers")
        return cls(
            session_id=session_id,
            user_id=s("user_id"),
            user_email=s("user_email"),
            client_id=s("client_id"),
            connection_id=s("connection_id"),
            stream_name=s("stream_name"),
            provider=s("provider"),
            mode=s("mode"),
            started_at=ffloat("started_at") or 0.0,
            last_chunk_at=ffloat("last_chunk_at") or 0.0,
            finalized_at=ffloat("finalized_at"),
            completed_at=ffloat("completed_at"),
            speech_detected_at=s("speech_detected_at"),
            chunks_published=fint("chunks_published"),
            transcription_seconds_sent=ffloat("transcription_seconds_sent") or 0.0,
            transcription_provider_status=s("transcription_provider_status"),
            transcription_provider_connected_at=(
                ffloat("transcription_provider_connected_at") or 0.0
            ),
            transcription_last_audio_sent_at=(
                ffloat("transcription_last_audio_sent_at") or 0.0
            ),
            transcription_last_message_at=(
                ffloat("transcription_last_message_at") or 0.0
            ),
            speech_detection_job_id=s("speech_detection_job_id"),
            audio_persistence_job_id=s("audio_persistence_job_id"),
            websocket_connected=s("websocket_connected") == "true",
            status=_coerce_enum(SessionStatus, d.get("status")),
            speaker_check_status=_coerce_enum(
                SpeakerCheckStatus, d.get("speaker_check_status")
            ),
            audio_format=fjson("audio_format", None),
            markers=fjson("markers", []),
            completion_reason=s("completion_reason"),
            conversation_close_requested=s("conversation_close_requested"),
            transcription_error=s("transcription_error"),
            last_event=s("last_event"),
            identified_speakers=speakers_raw.split(",") if speakers_raw else [],
        )


class SessionStore:
    """Typed facade over the ``audio:session:{id}`` Redis hash and its sibling keys.

    Wraps an existing async redis client. All hash mutations that must be observed
    together are issued as a single ``hset(mapping=...)``; signal publishes always
    follow the hash write so a subscriber observes the updated hash.
    """

    def __init__(self, redis_client):
        self._redis = redis_client

    # ------------------------------------------------------------------ keys
    @staticmethod
    def _key(session_id: str) -> str:
        return _SESSION_KEY.format(session_id)

    @staticmethod
    def _count_key(session_id: str) -> str:
        return _CONV_COUNT_KEY.format(session_id)

    async def _publish_signal(self, session_id: str, payload: dict) -> None:
        await self._redis.publish(
            _SIGNAL_CHANNEL.format(session_id), json.dumps(payload)
        )

    # -------------------------------------------------------------- lifecycle
    async def init_session(
        self,
        session_id: str,
        *,
        user_id: str,
        client_id: str,
        stream_name: str,
        user_email: str = "",
        connection_id: str = "",
        mode: str = "streaming",
        provider: str = "deepgram",
    ) -> None:
        """Create the session hash (status=active). Single atomic write.

        ``session_id`` is stable across reconnects (it equals ``client_id``), so a
        reconnecting device re-initializes the SAME hash. Connection-scoped fields
        must therefore be RESET here, not left to carry over from the previous
        connection: a stale ``transcription_error`` would make the next speech
        detection job break on iteration 1, a stale ``transcription_seconds_sent``
        would shift the new connection's transcript timestamps by a bogus offset,
        and a stale ``completion_reason`` would misclassify the new connection's
        diagnostics. They are explicitly cleared in the mapping below.
        """
        now = str(time.time())
        await self._redis.hset(
            self._key(session_id),
            mapping={
                "user_id": user_id,
                "user_email": user_email,
                "client_id": client_id,
                "connection_id": connection_id,
                "stream_name": stream_name,
                "provider": provider,
                "mode": mode,
                "started_at": now,
                "last_chunk_at": now,
                "chunks_published": "0",
                "speech_detection_job_id": "",
                "audio_persistence_job_id": "",
                "websocket_connected": "true",
                "status": SessionStatus.ACTIVE.value,
                # Connection-scoped — reset on every (re)connect (see docstring).
                "transcription_error": "",
                "transcription_seconds_sent": "0",
                "transcription_provider_status": "disconnected",
                "transcription_provider_connected_at": "0",
                "transcription_last_audio_sent_at": "0",
                "transcription_last_message_at": "0",
                "completion_reason": "",
            },
        )

    async def mark_finalizing(
        self, session_id: str, completion_reason: Optional[str] = None
    ) -> None:
        """Set status=finalizing (+reason) atomically, then publish a finalize signal."""
        mapping = {
            "status": SessionStatus.FINALIZING.value,
            "finalized_at": str(time.time()),
        }
        if completion_reason:
            mapping["completion_reason"] = completion_reason
            if completion_reason == "websocket_disconnect":
                mapping["websocket_connected"] = "false"
        await self._redis.hset(self._key(session_id), mapping=mapping)
        await self._publish_signal(
            session_id, {"type": "finalize", "reason": completion_reason or "unknown"}
        )

    async def mark_complete(self, session_id: str, reason: CompletionReason) -> None:
        """Set status=finished + completion_reason atomically, then publish a signal.

        This is the single source of truth for session completion: status and
        completion_reason are always written together so a worker never sees a
        finished status without its reason.
        """
        mapping = {
            "status": SessionStatus.FINISHED.value,
            "completed_at": str(time.time()),
            "completion_reason": reason,
        }
        if reason == "websocket_disconnect":
            mapping["websocket_connected"] = "false"
        await self._redis.hset(self._key(session_id), mapping=mapping)
        await self._publish_signal(
            session_id, {"type": "session_complete", "reason": reason}
        )
        logger.info(f"✅ Session {session_id[:12]} marked finished: {reason}")

    async def set_status_active(self, session_id: str) -> None:
        """Reset status back to active (recovery from a spurious finished signal)."""
        await self._redis.hset(
            self._key(session_id), "status", SessionStatus.ACTIVE.value
        )

    async def set_job_ids(
        self,
        session_id: str,
        *,
        speech_detection_job_id: Optional[str] = None,
        audio_persistence_job_id: Optional[str] = None,
    ) -> None:
        updates = {}
        if speech_detection_job_id:
            updates["speech_detection_job_id"] = speech_detection_job_id
        if audio_persistence_job_id:
            updates["audio_persistence_job_id"] = audio_persistence_job_id
        if updates:
            await self._redis.hset(self._key(session_id), mapping=updates)

    async def bump_chunk_count(self, session_id: str) -> None:
        """Increment chunks_published and refresh last_chunk_at."""
        key = self._key(session_id)
        await self._redis.hincrby(key, "chunks_published", 1)
        await self._redis.hset(key, "last_chunk_at", str(time.time()))

    async def expire_session(self, session_id: str, ttl: int) -> None:
        await self._redis.expire(self._key(session_id), ttl)

    async def persist_session(self, session_id: str) -> None:
        """Remove any TTL so the hash lives as long as the session is active.

        A TTL must never count down on a live session — if the hash expires
        mid-session it gets resurrected with partial fields (no client_id/status),
        stranding the speech-detection restart and leaking jobs. The session hash
        is cleaned up explicitly on end (expire_session) or disconnect.
        """
        await self._redis.persist(self._key(session_id))

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))

    # ----------------------------------------------------------- field writes
    async def set_audio_format(self, session_id: str, audio_format: dict) -> None:
        await self._redis.hset(
            self._key(session_id), "audio_format", json.dumps(audio_format)
        )

    async def set_markers(self, session_id: str, markers: list) -> None:
        await self._redis.hset(self._key(session_id), "markers", json.dumps(markers))

    async def set_transcription_error(self, session_id: str, message: str) -> None:
        await self._redis.hset(
            self._key(session_id),
            mapping={
                "transcription_error": message,
                "transcription_provider_status": "error",
            },
        )

    async def mark_transcription_provider_connected(self, session_id: str) -> None:
        now = str(time.time())
        await self._redis.hset(
            self._key(session_id),
            mapping={
                "transcription_provider_status": "connected",
                "transcription_provider_connected_at": now,
                "transcription_error": "",
            },
        )

    async def mark_transcription_audio_sent(self, session_id: str) -> None:
        await self._redis.hset(
            self._key(session_id), "transcription_last_audio_sent_at", str(time.time())
        )

    async def mark_transcription_provider_message(self, session_id: str) -> None:
        await self._redis.hset(
            self._key(session_id), "transcription_last_message_at", str(time.time())
        )

    async def mark_transcription_provider_disconnected(self, session_id: str) -> None:
        key = self._key(session_id)
        status = await self._redis.hget(key, "transcription_provider_status")
        if _to_str(status) != "error":
            await self._redis.hset(key, "transcription_provider_status", "disconnected")

    async def set_transcription_seconds(self, session_id: str, seconds: float) -> None:
        """Persist the streaming provider's session-relative audio clock."""
        await self._redis.hset(
            self._key(session_id), "transcription_seconds_sent", str(seconds)
        )

    async def request_close(self, session_id: str, reason: str) -> bool:
        """Flag the current conversation to close (session stays alive). Publishes a signal.

        Returns False if the session doesn't exist.
        """
        key = self._key(session_id)
        if not await self._redis.exists(key):
            return False
        await self._redis.hset(key, "conversation_close_requested", reason)
        await self._publish_signal(
            session_id, {"type": "close_requested", "reason": reason}
        )
        return True

    async def take_close_request(self, session_id: str) -> Optional[str]:
        """Read and consume (delete) the conversation_close_requested flag."""
        key = self._key(session_id)
        raw = await self._redis.hget(key, "conversation_close_requested")
        reason = _to_str(raw)
        if reason is not None:
            await self._redis.hdel(key, "conversation_close_requested")
        return reason

    async def record_event(self, session_id: str, event: str) -> None:
        """Append a timestamped event marker to ``last_event`` (``{event}:{iso}``)."""
        await self._redis.hset(
            self._key(session_id), "last_event", f"{event}:{_iso_now()}"
        )

    async def set_speech_detected_at(self, session_id: str) -> None:
        await self._redis.hset(self._key(session_id), "speech_detected_at", _iso_now())

    async def set_speaker_check(
        self, session_id: str, status: SpeakerCheckStatus
    ) -> None:
        await self._redis.hset(
            self._key(session_id), "speaker_check_status", status.value
        )

    async def set_identified_speakers(self, session_id: str, names: list) -> None:
        await self._redis.hset(
            self._key(session_id), "identified_speakers", ",".join(names)
        )

    # ----------------------------------------------------------- targeted reads
    async def get_status(self, session_id: str) -> Optional[SessionStatus]:
        raw = await self._redis.hget(self._key(session_id), "status")
        return _coerce_enum(SessionStatus, _to_str(raw))

    async def is_websocket_connected(self, session_id: str) -> bool:
        raw = await self._redis.hget(self._key(session_id), "websocket_connected")
        return _to_str(raw) == "true"

    async def get_last_chunk_at(self, session_id: str) -> Optional[float]:
        raw = await self._redis.hget(self._key(session_id), "last_chunk_at")
        s = _to_str(raw)
        if s in (None, ""):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    async def get_audio_format(self, session_id: str) -> tuple[int, int, int]:
        """``(sample_rate, channels, sample_width)``; 16 kHz/mono/16-bit on miss/parse-error."""
        try:
            raw = await self._redis.hget(self._key(session_id), "audio_format")
            s = _to_str(raw)
            if s:
                fmt = json.loads(s)
                return (
                    int(fmt.get("rate", 16000)),
                    int(fmt.get("channels", 1)),
                    int(fmt.get("width", 2)),
                )
        except Exception as e:
            logger.warning(
                f"Failed to read audio_format for session {session_id}, using defaults: {e}"
            )
        return _DEFAULT_AUDIO_FORMAT

    async def get_markers(self, session_id: str) -> list:
        raw = await self._redis.hget(self._key(session_id), "markers")
        s = _to_str(raw)
        if not s:
            return []
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            logger.warning(f"Malformed markers JSON for session {session_id}")
            return []

    async def get_completion_reason(self, session_id: str) -> str:
        raw = await self._redis.hget(self._key(session_id), "completion_reason")
        return _to_str(raw) or ""

    async def get_transcription_error(self, session_id: str) -> Optional[str]:
        raw = await self._redis.hget(self._key(session_id), "transcription_error")
        return _to_str(raw)

    async def get_transcription_seconds(self, session_id: str) -> float:
        raw = await self._redis.hget(
            self._key(session_id), "transcription_seconds_sent"
        )
        s = _to_str(raw)
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0

    async def get_status_ws_reason(
        self, session_id: str
    ) -> tuple[Optional[SessionStatus], bool, str]:
        """Batched read of ``(status, websocket_connected, completion_reason)``."""
        raw = await self._redis.hmget(
            self._key(session_id),
            "status",
            "websocket_connected",
            "completion_reason",
        )
        status = _coerce_enum(SessionStatus, _to_str(raw[0]))
        ws_connected = _to_str(raw[1]) == "true"
        reason = _to_str(raw[2]) or ""
        return status, ws_connected, reason

    # -------------------------------------------------------- whole-object reads
    async def exists(self, session_id: str) -> bool:
        return bool(await self._redis.exists(self._key(session_id)))

    async def read(self, session_id: str) -> Optional[SessionView]:
        raw = await self._redis.hgetall(self._key(session_id))
        if not raw:
            return None
        return SessionView.from_hash(session_id, raw)

    async def scan_session_ids(
        self, match: str = "audio:session:*", count: int = 100
    ) -> AsyncIterator[str]:
        """Yield bare session ids by cursor-scanning the session-hash keyspace."""
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(cursor, match=match, count=count)
            for key in keys:
                key_str = _to_str(key)
                if key_str:
                    yield key_str.removeprefix("audio:session:")
            if cursor == 0:
                break

    async def iter_views(
        self, limit: Optional[int] = None
    ) -> AsyncIterator[SessionView]:
        """Yield a :class:`SessionView` per existing session (skips emptied hashes)."""
        n = 0
        async for session_id in self.scan_session_ids():
            view = await self.read(session_id)
            if view is None:
                continue
            yield view
            n += 1
            if limit is not None and n >= limit:
                return

    # --------------------------------------------------------- conversation count
    async def get_conversation_count(self, session_id: str) -> int:
        raw = await self._redis.get(self._count_key(session_id))
        s = _to_str(raw)
        return int(s) if s else 0

    async def increment_conversation_count(self, session_id: str) -> int:
        key = self._count_key(session_id)
        count = await self._redis.incr(key)
        await self._redis.expire(key, _CONV_COUNT_TTL)
        return count


def get_session_store() -> SessionStore:
    """Convenience for FastAPI/singleton callers: wrap the producer's shared client."""
    # Lazy import: circular dependency (producer imports SessionStore at module top)
    from advanced_omi_backend.services.audio_stream.producer import (
        get_audio_stream_producer,
    )

    return SessionStore(get_audio_stream_producer().redis_client)
