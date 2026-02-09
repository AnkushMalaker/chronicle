"""Simplified ClientState that only manages state, no processing.

This module provides a lightweight ClientState class that tracks conversation
state, timestamps, and speech segments. All processing is handled at the
application level by the ProcessorManager.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from wyoming.audio import AudioChunk

# Get loggers
audio_logger = logging.getLogger("audio_processing")

# Configuration constants
NEW_CONVERSATION_TIMEOUT_MINUTES = float(os.getenv("NEW_CONVERSATION_TIMEOUT_MINUTES", "1.5"))


class ClientState:
    """Manages conversation state for a single client connection."""

    def __init__(
        self,
        client_id: str,
        chunk_dir: Path,
        user_id: str,
        user_email: Optional[str] = None,
    ):
        self.client_id = client_id
        self.connected = True
        self.chunk_dir = chunk_dir

        # Store user data for memory processing
        self.user_id = user_id
        self.user_email = user_email

        # Conversation state tracking
        self.last_transcript_time: Optional[float] = None
        self.conversation_start_time: float = time.time()
        self.current_audio_uuid: Optional[str] = None

        # Speech segment tracking for audio cropping
        self.speech_segments: Dict[str, List[Tuple[float, float]]] = {}
        self.current_speech_start: Dict[str, Optional[float]] = {}

        # NOTE: Removed in-memory transcript storage for single source of truth
        # Transcripts are stored only in MongoDB via TranscriptionManager

        # Markers (e.g., button events) collected during the session
        self.markers: List[dict] = []

        # Track if conversation has been closed
        self.conversation_closed: bool = False

        # Audio configuration - sample rate for this client's audio stream
        self.sample_rate: Optional[int] = None

        # Debug tracking
        self.transaction_id: Optional[str] = None

        audio_logger.info(f"Created client state for {client_id}")

    def update_audio_received(self, chunk: AudioChunk):
        """Update state when audio is received."""
        # Check if we should start a new conversation
        if self.should_start_new_conversation():
            asyncio.create_task(self.start_new_conversation())

    def set_current_audio_uuid(self, audio_uuid: str):
        """Set the current audio UUID when processor creates a new file."""
        self.current_audio_uuid = audio_uuid
        self.conversation_closed = False  # Reset for new audio file

    def record_speech_start(self, audio_uuid: str, timestamp: float):
        """Record the start of a speech segment."""
        self.current_speech_start[audio_uuid] = timestamp
        audio_logger.info(f"Recorded speech start for {audio_uuid}: {timestamp}")

    def record_speech_end(self, audio_uuid: str, timestamp: float):
        """Record the end of a speech segment."""
        if (
            audio_uuid in self.current_speech_start
            and self.current_speech_start[audio_uuid] is not None
        ):
            start_time = self.current_speech_start[audio_uuid]
            if start_time is not None:
                if audio_uuid not in self.speech_segments:
                    self.speech_segments[audio_uuid] = []
                self.speech_segments[audio_uuid].append((start_time, timestamp))
                self.current_speech_start[audio_uuid] = None
                duration = timestamp - start_time
                audio_logger.info(
                    f"Recorded speech segment for {audio_uuid}: {start_time:.3f} -> {timestamp:.3f} "
                    f"(duration: {duration:.3f}s)"
                )
        else:
            audio_logger.warning(f"Speech end recorded for {audio_uuid} but no start time found")

    def update_transcript_received(self):
        """Update timestamp when transcript is received (for timeout detection)."""
        self.last_transcript_time = time.time()

    def add_marker(self, marker: dict) -> None:
        """Add a marker (e.g., button event) to the current session."""
        self.markers.append(marker)

    def should_start_new_conversation(self) -> bool:
        """Check if we should start a new conversation based on timeout."""
        if self.last_transcript_time is None:
            return False

        current_time = time.time()
        time_since_last_transcript = current_time - self.last_transcript_time
        timeout_seconds = NEW_CONVERSATION_TIMEOUT_MINUTES * 60

        return time_since_last_transcript > timeout_seconds

    async def close_current_conversation(self):
        """Clean up in-memory speech segments for the current conversation."""
        if self.conversation_closed:
            audio_logger.debug(
                f"🔒 Conversation already closed for client {self.client_id}, skipping"
            )
            return

        self.conversation_closed = True

        if not self.current_audio_uuid:
            return

        audio_logger.info(f"🔒 Closing conversation state for client {self.client_id}")

        if self.current_audio_uuid in self.speech_segments:
            del self.speech_segments[self.current_audio_uuid]
        if self.current_audio_uuid in self.current_speech_start:
            del self.current_speech_start[self.current_audio_uuid]

    async def start_new_conversation(self):
        """Start a new conversation by closing current and resetting state."""
        await self.close_current_conversation()

        # Reset conversation state
        self.current_audio_uuid = None
        self.conversation_start_time = time.time()
        self.last_transcript_time = None
        self.conversation_closed = False
        self.markers = []

        audio_logger.info(f"Client {self.client_id}: Started new conversation")

    async def disconnect(self):
        """Clean disconnect of client state."""
        if not self.connected:
            return

        self.connected = False
        audio_logger.info(f"Disconnecting client {self.client_id}")

        # Close current conversation
        await self.close_current_conversation()

        # Clean up state
        self.speech_segments.clear()
        self.current_speech_start.clear()

        audio_logger.info(f"Client {self.client_id} disconnected and cleaned up")
