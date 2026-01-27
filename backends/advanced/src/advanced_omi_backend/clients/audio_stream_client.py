"""WebSocket client for audio streaming using Wyoming protocol.

This client mirrors the protocol implementation in websocket_controller.py
and can be used for integration testing and external integrations.

Protocol flow:
1. Connect to WebSocket with token and device_name
2. Receive "ready" message from server (PCM endpoint only)
3. Send "audio-start" with format and mode
4. Send audio chunks (Wyoming protocol or raw binary)
5. Send "audio-stop" to finalize session

Example usage (blocking):
    ```python
    import asyncio
    from advanced_omi_backend.clients import AudioStreamClient

    async def main():
        client = AudioStreamClient("http://localhost:8000", "your-jwt-token")
        await client.connect()
        await client.stream_wav_file("/path/to/audio.wav")
        await client.close()

    asyncio.run(main())
    ```

Example usage (non-blocking for testing):
    ```python
    from advanced_omi_backend.clients.audio_stream_client import StreamManager

    manager = StreamManager()
    stream_id = manager.start_stream("http://localhost:8000", "token", "device")
    manager.send_chunks_from_file(stream_id, "/path/to/audio.wav", num_chunks=10)
    # ... do other things while stream is open ...
    manager.stop_stream(stream_id)
    ```
"""

import asyncio
import json
import logging
import threading
import uuid
import wave
from pathlib import Path
from typing import Dict, Optional, Union

import websockets
from websockets.client import WebSocketClientProtocol

from advanced_omi_backend.constants import OMI_CHANNELS, OMI_SAMPLE_RATE, OMI_SAMPLE_WIDTH

logger = logging.getLogger(__name__)


class AudioStreamClient:
    """WebSocket client for streaming audio using Wyoming protocol.

    This client implements the same protocol as the server expects in
    websocket_controller.py, ensuring consistency between client and server.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        device_name: str = "python-client",
        endpoint: str = "ws?codec=pcm",
    ):
        """Initialize the audio stream client.

        Args:
            base_url: Base URL of the backend (e.g., "http://localhost:8000")
            token: JWT authentication token
            device_name: Device name for client identification
            endpoint: WebSocket endpoint ("ws?codec=pcm" or "ws?codec=opus")
        """
        self.base_url = base_url
        self.token = token
        self.device_name = device_name
        self.endpoint = endpoint
        self.ws: Optional[WebSocketClientProtocol] = None
        self.chunk_count = 0
        self.total_bytes = 0

    @property
    def ws_url(self) -> str:
        """Build WebSocket URL from base URL."""
        url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        # Check if endpoint already has query params
        separator = "&" if "?" in self.endpoint else "?"
        return f"{url}/{self.endpoint}{separator}token={self.token}&device_name={self.device_name}"

    async def connect(self, wait_for_ready: bool = True) -> WebSocketClientProtocol:
        """Connect to the WebSocket endpoint.

        Args:
            wait_for_ready: If True, wait for "ready" message from server (PCM endpoint)

        Returns:
            The WebSocket connection

        Raises:
            RuntimeError: If connection fails or ready message not received
        """
        logger.info(f"Connecting to {self.ws_url}")
        self.ws = await websockets.connect(self.ws_url)
        logger.info("WebSocket connected")

        if wait_for_ready and "codec=pcm" in self.endpoint:
            # PCM codec sends "ready" message after auth (line 261-268 in websocket_controller.py)
            ready_msg = await self.ws.recv()
            ready = json.loads(ready_msg.strip() if isinstance(ready_msg, str) else ready_msg.decode().strip())
            if ready.get("type") != "ready":
                raise RuntimeError(f"Expected 'ready' message, got: {ready}")
            logger.info("Received ready message from server")

        return self.ws

    async def send_audio_start(
        self,
        recording_mode: str = "streaming",
        sample_rate: int = OMI_SAMPLE_RATE,
        sample_width: int = OMI_SAMPLE_WIDTH,
        channels: int = OMI_CHANNELS,
        always_persist: bool = False,
    ) -> None:
        """Send Wyoming audio-start event.

        Args:
            recording_mode: "streaming" or "batch"
            sample_rate: Audio sample rate in Hz (default: 16000)
            sample_width: Bytes per sample (default: 2 for 16-bit)
            channels: Number of audio channels (default: 1)
            always_persist: Save audio even if transcription fails (default: False)

        Note:
            The mode is inside the "data" dict, matching _handle_audio_session_start
            in websocket_controller.py (line 618).
        """
        if not self.ws:
            raise RuntimeError("Not connected. Call connect() first.")

        header = {
            "type": "audio-start",
            "data": {
                "rate": sample_rate,
                "width": sample_width,
                "channels": channels,
                "mode": recording_mode,
                "always_persist": always_persist,
            },
            "payload_length": None,
        }
        print(f"🔵 CLIENT: Sending audio-start message: {header}")
        logger.info(f"🔵 CLIENT: Sending audio-start message: {header}")
        await self.ws.send(json.dumps(header) + "\n")
        print(f"✅ CLIENT: Sent audio-start with mode={recording_mode}, always_persist={always_persist}")
        logger.info(f"✅ CLIENT: Sent audio-start with mode={recording_mode}, always_persist={always_persist}")

    async def send_audio_chunk_wyoming(
        self,
        audio_data: bytes,
        sample_rate: int = OMI_SAMPLE_RATE,
        sample_width: int = OMI_SAMPLE_WIDTH,
        channels: int = OMI_CHANNELS,
    ) -> None:
        """Send audio chunk using Wyoming protocol (JSON header + binary payload).

        This matches the handler at lines 979-1007 in websocket_controller.py.

        Args:
            audio_data: Raw PCM audio bytes
            sample_rate: Audio sample rate in Hz
            sample_width: Bytes per sample
            channels: Number of audio channels
        """
        if not self.ws:
            raise RuntimeError("Not connected. Call connect() first.")

        header = {
            "type": "audio-chunk",
            "payload_length": len(audio_data),
            "data": {
                "rate": sample_rate,
                "width": sample_width,
                "channels": channels,
            },
        }
        # Send JSON header followed by binary payload
        await self.ws.send(json.dumps(header) + "\n")
        await self.ws.send(audio_data)

        self.chunk_count += 1
        self.total_bytes += len(audio_data)

        if self.chunk_count <= 3 or self.chunk_count % 100 == 0:
            logger.debug(f"Sent audio chunk #{self.chunk_count}: {len(audio_data)} bytes")

    async def send_audio_chunk_raw(self, audio_data: bytes) -> None:
        """Send raw binary audio without Wyoming header (legacy mode).

        This matches the handler at lines 1016-1035 in websocket_controller.py.

        Args:
            audio_data: Raw PCM audio bytes
        """
        if not self.ws:
            raise RuntimeError("Not connected. Call connect() first.")

        await self.ws.send(audio_data)

        self.chunk_count += 1
        self.total_bytes += len(audio_data)

    async def send_audio_stop(self) -> None:
        """Send Wyoming audio-stop event to finalize the session."""
        if not self.ws:
            raise RuntimeError("Not connected. Call connect() first.")

        header = {"type": "audio-stop"}
        await self.ws.send(json.dumps(header) + "\n")
        logger.info(f"Sent audio-stop (total: {self.chunk_count} chunks, {self.total_bytes} bytes)")

    async def send_ping(self) -> None:
        """Send keepalive ping."""
        if not self.ws:
            raise RuntimeError("Not connected. Call connect() first.")

        header = {"type": "ping", "payload_length": None}
        await self.ws.send(json.dumps(header) + "\n")
        logger.debug("Sent ping")

    async def stream_wav_file(
        self,
        wav_path: Union[str, Path],
        chunk_duration_ms: int = 100,
        use_wyoming: bool = True,
        recording_mode: str = "streaming",
        realtime_factor: float = 0.1,
        always_persist: bool = False,
    ) -> int:
        """Stream a WAV file in chunks, simulating real-time audio.

        Args:
            wav_path: Path to the WAV file
            chunk_duration_ms: Duration of each chunk in milliseconds
            use_wyoming: If True, use Wyoming protocol; if False, send raw binary
            recording_mode: "streaming" or "batch"
            realtime_factor: Fraction of real-time to simulate (0.1 = 10x speed)
            always_persist: Save audio even if transcription fails (default: False)

        Returns:
            Number of chunks sent
        """
        wav_path = Path(wav_path)
        if not wav_path.exists():
            raise FileNotFoundError(f"WAV file not found: {wav_path}")

        with wave.open(str(wav_path), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()

            logger.info(
                f"Streaming {wav_path.name}: {sample_rate}Hz, {channels}ch, {sample_width * 8}-bit"
            )

            # Calculate chunk size
            bytes_per_sample = sample_width * channels
            samples_per_chunk = int(sample_rate * chunk_duration_ms / 1000)

            # Send audio-start
            await self.send_audio_start(
                recording_mode=recording_mode,
                sample_rate=sample_rate,
                sample_width=sample_width,
                channels=channels,
                always_persist=always_persist,
            )

            # Reset counters
            self.chunk_count = 0
            self.total_bytes = 0

            # Stream chunks
            while True:
                chunk = wav.readframes(samples_per_chunk)
                if not chunk:
                    break

                if use_wyoming:
                    await self.send_audio_chunk_wyoming(
                        chunk,
                        sample_rate=sample_rate,
                        sample_width=sample_width,
                        channels=channels,
                    )
                else:
                    await self.send_audio_chunk_raw(chunk)

                # Simulate real-time delay
                if realtime_factor > 0:
                    await asyncio.sleep(chunk_duration_ms / 1000 * realtime_factor)

            # Send audio-stop
            await self.send_audio_stop()

            logger.info(f"Finished streaming: {self.chunk_count} chunks, {self.total_bytes} bytes")
            return self.chunk_count

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self.ws:
            try:
                # Add timeout to WebSocket close to prevent hanging
                await asyncio.wait_for(self.ws.close(), timeout=2.0)
                logger.info("WebSocket connection closed cleanly")
            except asyncio.TimeoutError:
                logger.warning("WebSocket close timed out after 2s, forcing close")
                # Force close without waiting for handshake
                if hasattr(self.ws, 'transport') and self.ws.transport:
                    self.ws.transport.close()
            except Exception as e:
                logger.error(f"Error during WebSocket close: {e}")
            finally:
                self.ws = None

    async def __aenter__(self) -> "AudioStreamClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()


# Synchronous wrapper for Robot Framework and other sync contexts
def stream_audio_file(
    base_url: str,
    token: str,
    wav_path: str,
    device_name: str = "robot-test",
    recording_mode: str = "streaming",
    use_wyoming: bool = True,
    always_persist: bool = False,
) -> int:
    """Synchronous wrapper for streaming audio file.

    This function is designed for use with Robot Framework or other
    synchronous test frameworks.

    Args:
        base_url: Base URL of the backend
        token: JWT authentication token
        wav_path: Path to WAV file
        device_name: Device name for client identification
        recording_mode: "streaming" or "batch"
        use_wyoming: If True, use Wyoming protocol
        always_persist: Save audio even if transcription fails (default: False)

    Returns:
        Number of chunks sent
    """

    async def _run() -> int:
        async with AudioStreamClient(base_url, token, device_name) as client:
            return await client.stream_wav_file(
                wav_path,
                use_wyoming=use_wyoming,
                recording_mode=recording_mode,
                always_persist=always_persist,
            )

    return asyncio.run(_run())


class StreamSession:
    """Holds state for an active streaming session."""

    def __init__(
        self,
        stream_id: str,
        client: AudioStreamClient,
        loop: asyncio.AbstractEventLoop,
        thread: threading.Thread,
    ):
        self.stream_id = stream_id
        self.client = client
        self.loop = loop
        self.thread = thread
        self.connected = False
        self.audio_started = False
        self.chunk_count = 0
        self.error: Optional[str] = None


class StreamManager:
    """Manages multiple non-blocking audio streams for testing.

    This allows tests to start a stream, perform checks while streaming,
    and then stop the stream - mimicking real client behavior.

    Example:
        manager = StreamManager()
        stream_id = manager.start_stream(base_url, token, "test-device")
        manager.send_chunks_from_file(stream_id, "audio.wav", num_chunks=10)
        # ... check jobs, verify state ...
        manager.stop_stream(stream_id)
    """

    def __init__(self):
        self._sessions: Dict[str, StreamSession] = {}

    def start_stream(
        self,
        base_url: str,
        token: str,
        device_name: str = "robot-test",
        recording_mode: str = "streaming",
        always_persist: bool = False,
    ) -> str:
        """Start a new audio stream (non-blocking).

        Args:
            base_url: Backend URL
            token: JWT token
            device_name: Device name for client ID
            recording_mode: "streaming" or "batch"
            always_persist: Save audio even if transcription fails (default: False)

        Returns:
            stream_id: Unique ID for this stream session
        """
        stream_id = str(uuid.uuid4())[:8]

        # Create event loop for this stream's thread
        loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()

        # Create client
        client = AudioStreamClient(base_url, token, device_name)

        session = StreamSession(stream_id, client, loop, thread)
        self._sessions[stream_id] = session

        # Connect and send audio-start
        async def _connect_and_start():
            try:
                logger.info(f"🔵 CLIENT: Stream {stream_id} connecting for {device_name}...")
                await client.connect()
                session.connected = True
                logger.info(f"✅ CLIENT: Stream {stream_id} connected, sending audio-start...")
                await client.send_audio_start(recording_mode=recording_mode, always_persist=always_persist)
                session.audio_started = True
                logger.info(f"✅ CLIENT: Stream {stream_id} started for {device_name}")
            except Exception as e:
                session.error = str(e)
                logger.error(f"❌ CLIENT: Stream {stream_id} failed to start: {e}")

        future = asyncio.run_coroutine_threadsafe(_connect_and_start(), loop)
        future.result(timeout=10)  # Wait for connection

        if session.error:
            raise RuntimeError(f"Failed to start stream: {session.error}")

        return stream_id

    def send_chunks_from_file(
        self,
        stream_id: str,
        wav_path: str,
        num_chunks: Optional[int] = None,
        chunk_duration_ms: int = 100,
        realtime_pacing: bool = False,
    ) -> int:
        """Send audio chunks from a WAV file.

        Args:
            stream_id: Stream session ID
            wav_path: Path to WAV file
            num_chunks: Number of chunks to send (None = all)
            chunk_duration_ms: Duration per chunk in ms
            realtime_pacing: If True, sleep between chunks to simulate real-time streaming

        Returns:
            Number of chunks sent
        """
        session = self._sessions.get(stream_id)
        if not session:
            raise ValueError(f"Unknown stream_id: {stream_id}")

        if not session.audio_started:
            raise RuntimeError("Stream not started")

        wav_path = Path(wav_path)
        if not wav_path.exists():
            raise FileNotFoundError(f"WAV file not found: {wav_path}")

        async def _send_chunks() -> int:
            with wave.open(str(wav_path), "rb") as wav:
                sample_rate = wav.getframerate()
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()

                samples_per_chunk = int(sample_rate * chunk_duration_ms / 1000)
                chunks_sent = 0
                chunk_duration_seconds = chunk_duration_ms / 1000.0

                while True:
                    if num_chunks is not None and chunks_sent >= num_chunks:
                        break

                    chunk = wav.readframes(samples_per_chunk)
                    if not chunk:
                        break

                    await session.client.send_audio_chunk_wyoming(
                        chunk,
                        sample_rate=sample_rate,
                        sample_width=sample_width,
                        channels=channels,
                    )
                    chunks_sent += 1
                    session.chunk_count += 1

                    # Optional: Sleep to maintain real-time pacing
                    if realtime_pacing:
                        await asyncio.sleep(chunk_duration_seconds)

                return chunks_sent

        future = asyncio.run_coroutine_threadsafe(_send_chunks(), session.loop)
        return future.result(timeout=60)

    def stop_stream(self, stream_id: str) -> int:
        """Stop a stream and close the connection.

        Args:
            stream_id: Stream session ID

        Returns:
            Total chunks sent during this session
        """
        session = self._sessions.get(stream_id)
        if not session:
            raise ValueError(f"Unknown stream_id: {stream_id}")

        async def _stop():
            if session.audio_started:
                await session.client.send_audio_stop()
            await session.client.close()

        future = asyncio.run_coroutine_threadsafe(_stop(), session.loop)
        future.result(timeout=10)

        # Stop the event loop
        session.loop.call_soon_threadsafe(session.loop.stop)
        session.thread.join(timeout=5)

        total_chunks = session.chunk_count
        del self._sessions[stream_id]

        logger.info(f"Stream {stream_id} stopped, sent {total_chunks} chunks")
        return total_chunks

    def close_stream_without_stop(self, stream_id: str) -> int:
        """Close WebSocket connection without sending audio-stop event.

        This simulates abrupt disconnection (network failure, client crash)
        and should trigger websocket_disconnect end_reason.

        Args:
            stream_id: Stream session ID

        Returns:
            Total chunks sent during this session
        """
        session = self._sessions.get(stream_id)
        if not session:
            raise ValueError(f"Unknown stream_id: {stream_id}")

        async def _close_abruptly():
            # Just close the connection without audio-stop
            await session.client.close()

        future = asyncio.run_coroutine_threadsafe(_close_abruptly(), session.loop)
        future.result(timeout=10)

        # Stop the event loop
        session.loop.call_soon_threadsafe(session.loop.stop)
        session.thread.join(timeout=5)

        total_chunks = session.chunk_count
        del self._sessions[stream_id]

        logger.info(f"Stream {stream_id} closed abruptly (no audio-stop), sent {total_chunks} chunks")
        return total_chunks

    def get_session(self, stream_id: str) -> Optional[StreamSession]:
        """Get session info for a stream."""
        return self._sessions.get(stream_id)

    def cleanup_all(self):
        """Stop all active streams."""
        for stream_id in list(self._sessions.keys()):
            try:
                self.stop_stream(stream_id)
            except Exception as e:
                logger.warning(f"Error stopping stream {stream_id}: {e}")
