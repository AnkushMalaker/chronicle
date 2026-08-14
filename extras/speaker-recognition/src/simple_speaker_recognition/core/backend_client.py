"""Client for fetching audio from Chronicle backend."""

import io
import logging
import time
import wave
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

        # Extended timeout for audio fetching. A corpus request can span many
        # independently bounded 10-minute neural passes; assembling a multi-hour WAV
        # at the backend legitimately takes longer than the former 60-second read cap.
        # A measured ten-hour capture took 527 seconds to reconstruct after the
        # capture-claim cutover, so the advertised twelve-hour request bound needs more
        # than ten minutes. Keep the transfer finite without making the independently
        # bounded neural pass itself any larger.
        self.audio_timeout = httpx.Timeout(
            connect=10.0, read=900.0, write=30.0, pool=10.0
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
        # Request WAV explicitly — the endpoint defaults to ogg/opus, whose
        # pre-skip (312 samples @ 48kHz) makes header duration exceed decodable
        # samples and trips pyannote's strict sample-count check on the last chunk.
        params = {"start": start, "format": "wav"}
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

    async def get_audio_timeline(
        self,
        conversation_id: str,
        token: str,
        *,
        total_duration: float,
        audio_ranges: list[tuple[float, float]],
    ) -> bytes:
        """Rebuild a clock-faithful WAV, representing missing chunks as silence.

        Chronicle refuses to reconstruct one range across a real chunk gap because
        concatenating the available chunks would compress wall-clock time.  Pyannote
        still needs one waveform whose sample positions line up with transcript word
        timestamps, so fetch each continuous island and place it at its original
        offset in a zero-filled timeline.
        """
        if total_duration <= 0:
            raise ValueError("total_duration must be positive")

        normalized: list[tuple[float, float]] = []
        for raw_start, raw_end in sorted(audio_ranges):
            start = max(0.0, float(raw_start))
            end = min(float(total_duration), float(raw_end))
            if end <= start:
                continue
            # Chunk bounds are normally exactly adjacent.  Merge overlaps and
            # sub-millisecond rounding noise, but retain every material clock gap.
            if normalized and start <= normalized[-1][1] + 0.001:
                normalized[-1] = (normalized[-1][0], max(normalized[-1][1], end))
            else:
                normalized.append((start, end))
        if not normalized:
            raise ValueError("audio_ranges contain no usable audio")

        clips: list[tuple[float, float, bytes, int, int, int]] = []
        format_signature: tuple[int, int, int] | None = None
        for start, end in normalized:
            wav_bytes = await self.get_audio_segment(
                conversation_id,
                token,
                start=start,
                duration=end - start,
            )
            with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
                signature = (
                    wav_file.getnchannels(),
                    wav_file.getsampwidth(),
                    wav_file.getframerate(),
                )
                if format_signature is None:
                    format_signature = signature
                elif signature != format_signature:
                    raise ValueError(
                        "Audio format changes between reconstructed timeline islands"
                    )
                frames = wav_file.readframes(wav_file.getnframes())
            clips.append((start, end, frames, *signature))

        assert format_signature is not None
        channels, sample_width, sample_rate = format_signature
        frame_width = channels * sample_width
        total_frames = round(float(total_duration) * sample_rate)
        timeline = bytearray(total_frames * frame_width)

        for start, end, frames, *_signature in clips:
            start_frame = round(start * sample_rate)
            expected_frames = max(0, round((end - start) * sample_rate))
            available_frames = len(frames) // frame_width
            copy_frames = min(
                expected_frames, available_frames, total_frames - start_frame
            )
            if copy_frames <= 0:
                continue
            output_start = start_frame * frame_width
            output_end = output_start + copy_frames * frame_width
            timeline[output_start:output_end] = frames[: copy_frames * frame_width]

        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(timeline)
        return output.getvalue()

    async def close(self):
        """Close HTTP client and release resources."""
        await self.client.aclose()
        logger.debug("Backend client closed")
