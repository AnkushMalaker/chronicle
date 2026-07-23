"""Single source of truth for Redis key names used across the backend.

Redis keys were historically built as inline f-strings at each call site, which is
a misnaming hazard: a typo in one of a dozen sites silently points at a different
key. This module centralizes the key *names* as typed builder functions so a key
can be constructed in exactly one place.

It is intentionally dependency-light — stdlib only, no imports of ``workers``,
``services``, ``models``, or ``controllers`` — so it can be imported from anywhere
(including ``utils.conversation_utils``, which the RQ workers import, and
``SessionStore``) without risk of an import cycle.

**Scope (Stage 1):** the session-scoped pointer/lock keys and the
``speech_detection_job`` family. Other key families (``transcription:*``,
``audio:stream:*``, ``sse:*``, …) are still built inline and may migrate here later.

**Invariant:** every WebSocket recording has a unique ``session_id``. A client may
have many historical sessions, and each session owns an immutable raw-audio WAL.
"""

# --- session-scoped ---


def audio_session(session_id: str) -> str:
    """Hash holding the cross-process session state (owned by ``SessionStore``)."""
    return f"audio:session:{session_id}"


def session_signal(session_id: str) -> str:
    """Pub/sub channel for session lifecycle signals."""
    return f"session:signal:{session_id}"


def session_conversation_count(session_id: str) -> str:
    """Counter of conversations opened during the session."""
    return f"session:conversation_count:{session_id}"


def conversation_current(session_id: str) -> str:
    """Pointer to the session's current conversation (drives WAV rotation)."""
    return f"conversation:current:{session_id}"


def conversation_create_lock(session_id: str) -> str:
    """Per-session lock serializing first-conversation creation across workers."""
    return f"conversation:create_lock:{session_id}"


def speech_detection_job(session_id: str) -> str:
    """Pointer to the live speech-detection job id for the session."""
    return f"speech_detection_job:{session_id}"


def speech_detection_enqueue_lock(session_id: str) -> str:
    """Single-flight lock guarding speech-detection job enqueue bursts."""
    return f"speech_detection_enqueue_lock:{session_id}"
