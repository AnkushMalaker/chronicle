"""Client for fetching audio from Chronicle backend."""

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class BackendClient:
    """Client for Chronicle backend API to fetch audio segments."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        """
        Initialize backend client.

        Args:
            base_url: Backend API base URL (e.g., http://host.docker.internal:8000)
            timeout: Request timeout in seconds (default: 30.0, used for metadata)
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        # Default timeout for metadata and other quick operations
        self.default_timeout = httpx.Timeout(timeout, read=timeout)

        # Extended timeout for audio fetching (large files can take time)
        # Connect: 10s, Read: 60s, Write: 30s, Pool: 10s
        # TODO: Adjust read timeout based on actual measured decode times
        self.audio_timeout = httpx.Timeout(
            connect=10.0, read=60.0, write=30.0, pool=10.0
        )

        # Use default timeout for the client (will override per-request)
        self.client = httpx.AsyncClient(timeout=self.default_timeout)

    async def get_conversation_metadata(self, conversation_id: str, token: str) -> dict:
        """
        Get conversation metadata (duration, etc.) without loading audio.

        Args:
            conversation_id: Conversation ID
            token: JWT token for authentication

        Returns:
            Dict with conversation_id, duration, created_at, has_audio

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.base_url}/api/conversations/{conversation_id}/metadata"
        headers = {"Authorization": f"Bearer {token}"}

        logger.debug(f"Fetching metadata for conversation {conversation_id[:12]}...")

        response = await self.client.get(url, headers=headers)
        response.raise_for_status()

        metadata = response.json()
        logger.info(
            f"Conversation {conversation_id[:12]}: "
            f"duration={metadata.get('duration', 0):.1f}s, "
            f"has_audio={metadata.get('has_audio', False)}"
        )

        return metadata

    async def get_audio_segment(
        self,
        conversation_id: str,
        token: str,
        start: float = 0.0,
        duration: Optional[float] = None,
    ) -> bytes:
        """
        Get audio segment as WAV bytes.

        Args:
            conversation_id: Conversation ID
            token: JWT token for authentication
            start: Start time in seconds (default: 0.0)
            duration: Duration in seconds (if None, returns all audio from start)

        Returns:
            WAV audio bytes

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.base_url}/api/conversations/{conversation_id}/audio-segments"
        params = {"start": start}
        if duration is not None:
            params["duration"] = duration
        headers = {"Authorization": f"Bearer {token}"}

        logger.debug(
            f"Fetching audio segment: conversation={conversation_id[:12]}, "
            f"start={start:.1f}s, duration={duration or 'all'}s"
        )

        fetch_start = time.time()

        # Use extended timeout for audio fetching (large files can take time)
        response = await self.client.get(
            url, params=params, headers=headers, timeout=self.audio_timeout
        )
        response.raise_for_status()

        wav_bytes = response.content
        fetch_time = time.time() - fetch_start

        logger.info(
            f"Fetched audio segment: {len(wav_bytes) / 1024 / 1024:.2f} MB "
            f"in {fetch_time:.2f}s (conversation={conversation_id[:12]}, "
            f"start={start:.1f}s, duration={duration or 'all'}s)"
        )

        return wav_bytes

    async def close(self):
        """Close HTTP client and release resources."""
        await self.client.aclose()
        logger.debug("Backend client closed")
