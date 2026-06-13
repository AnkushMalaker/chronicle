"""Lightweight per-connection ClientState.

Holds the in-memory state tied to a single live WebSocket connection: identity,
user info, session markers, and the streaming/batch bookkeeping the websocket
handlers maintain while a recording is in flight. Speech detection and
conversation lifecycle live in the Redis-streams / RQ-job pipeline, not here.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Get loggers
audio_logger = logging.getLogger("audio_processing")


@dataclass
class ClientState:
    """Connection-scoped state for a single client connection."""

    client_id: str
    user_id: str
    user_email: Optional[str] = None

    # Liveness flag, flipped by disconnect().
    connected: bool = True

    # Markers (e.g., button events) collected during the session,
    # drained onto the conversation when it is persisted.
    markers: List[dict] = field(default_factory=list)

    # Recording mode for the active audio session ("batch" or "streaming").
    recording_mode: str = "batch"

    # Streaming-mode session id, set when a streaming session is initialized and
    # reset to None when finalized. Doubles as the "session active" flag.
    stream_session_id: Optional[str] = None
    # NOTE: reserved — nothing populates this yet, so the streaming-finalize
    # buffer flush currently falls back to default 16kHz/mono/16-bit.
    stream_audio_format: Dict = field(default_factory=dict)

    # Batch-mode accumulator: audio is buffered here until a 30-minute roll or
    # an audio-stop, then flushed into a conversation.
    batch_started: bool = False
    batch_audio_chunks: List[bytes] = field(default_factory=list)
    batch_audio_format: Dict = field(default_factory=dict)
    batch_audio_bytes: int = 0
    batch_chunks_processed: int = 0

    def __post_init__(self) -> None:
        audio_logger.info(f"Created client state for {self.client_id}")

    def add_marker(self, marker: dict) -> None:
        """Add a marker (e.g., button event) to the current session."""
        self.markers.append(marker)

    async def disconnect(self) -> None:
        """Clean disconnect of client state."""
        if not self.connected:
            return

        self.connected = False
        audio_logger.info(f"Client {self.client_id} disconnected")
