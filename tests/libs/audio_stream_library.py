"""Robot Framework library for audio streaming via WebSocket.

This library wraps the AudioStreamClient from advanced_omi_backend.clients
for use in Robot Framework tests.

Usage in Robot Framework:
    Library    ../libs/audio_stream_library.py

    # Blocking mode (streams entire file)
    Stream Audio File    base_url=http://localhost:8000    token=${TOKEN}
    ...                  wav_path=/path/to/audio.wav    device_name=robot-test

    # Non-blocking mode (for testing during stream)
    ${stream_id}=    Start Audio Stream    http://localhost:8000    ${TOKEN}    device-name
    Send Audio Chunks    ${stream_id}    /path/to/audio.wav    num_chunks=10
    # ... perform checks while stream is open ...
    Stop Audio Stream    ${stream_id}
"""

import sys
from pathlib import Path
from typing import Optional

# Add the backend src to path so we can import the client
backend_src = Path(__file__).parent.parent.parent / "backends" / "advanced" / "src"
if str(backend_src) not in sys.path:
    sys.path.insert(0, str(backend_src))

from advanced_omi_backend.clients import AudioStreamClient
from advanced_omi_backend.clients.audio_stream_client import StreamManager, stream_audio_file as _stream_audio_file

# Module-level manager for non-blocking streams
_manager = StreamManager()


# =============================================================================
# Blocking Mode (simple, streams entire file)
# =============================================================================

def stream_audio_file(
    base_url: str,
    token: str,
    wav_path: str,
    device_name: str = "robot-test",
    recording_mode: str = "streaming",
    use_wyoming: bool = True,
    always_persist: bool = False,
) -> int:
    """Stream a WAV file via WebSocket (blocking)."""
    return _stream_audio_file(
        base_url=base_url,
        token=token,
        wav_path=wav_path,
        device_name=device_name,
        recording_mode=recording_mode,
        use_wyoming=use_wyoming,
        always_persist=always_persist,
    )


# =============================================================================
# Non-blocking Mode (for testing during stream)
# =============================================================================

def start_audio_stream(
    base_url: str,
    token: str,
    device_name: str = "robot-test",
    recording_mode: str = "streaming",
    always_persist: bool = False,
) -> str:
    """Start a new audio stream (non-blocking)."""
    return _manager.start_stream(
        base_url=base_url,
        token=token,
        device_name=device_name,
        recording_mode=recording_mode,
        always_persist=always_persist,
    )


def send_audio_chunks(
    stream_id: str,
    wav_path: str,
    num_chunks: Optional[int] = None,
    chunk_duration_ms: int = 100,
    realtime_pacing: bool = False,
) -> int:
    """Send audio chunks from a WAV file to an open stream.

    Args:
        stream_id: Stream session ID
        wav_path: Path to WAV file
        num_chunks: Number of chunks to send (None = all)
        chunk_duration_ms: Duration per chunk in ms
        realtime_pacing: If True, sleep between chunks to simulate real-time streaming

    Returns:
        Number of chunks sent
    """
    return _manager.send_chunks_from_file(
        stream_id=stream_id,
        wav_path=wav_path,
        num_chunks=num_chunks,
        chunk_duration_ms=chunk_duration_ms,
        realtime_pacing=realtime_pacing,
    )


def send_audio_stop_event(stream_id: str) -> None:
    """Send audio-stop event without closing the WebSocket connection.

    This is used to test the user_stopped end_reason scenario where
    the user manually stops recording but the connection remains open.
    """
    session = _manager._sessions.get(stream_id)
    if not session:
        raise ValueError(f"Stream {stream_id} not found")

    import asyncio

    async def _send_stop():
        try:
            await session.client.send_audio_stop()
            session.audio_stopped = True
        except Exception as e:
            session.error = str(e)
            raise

    # Run in the stream's event loop
    future = asyncio.run_coroutine_threadsafe(_send_stop(), session.loop)
    future.result(timeout=5)  # Wait for audio-stop to be sent

    if session.error:
        raise RuntimeError(f"Failed to send audio-stop: {session.error}")


def stop_audio_stream(stream_id: str) -> int:
    """Stop an audio stream and close the connection."""
    return _manager.stop_stream(stream_id)


def close_audio_stream_without_stop(stream_id: str) -> int:
    """Close WebSocket connection without sending audio-stop event.

    This simulates abrupt disconnection (network failure, client crash)
    and should trigger websocket_disconnect end_reason.
    """
    return _manager.close_stream_without_stop(stream_id)


def cleanup_all_streams():
    """Stop all active streams."""
    _manager.cleanup_all()


# =============================================================================
# Advanced Usage
# =============================================================================

def get_audio_stream_client(
    base_url: str,
    token: str,
    device_name: str = "robot-test",
) -> AudioStreamClient:
    """Get an AudioStreamClient instance for advanced usage."""
    return AudioStreamClient(base_url, token, device_name)
