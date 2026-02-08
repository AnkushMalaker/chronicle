#!/usr/bin/env python3
"""
- Listens on PORT (default 8989) for ESP32 client
- Decodes ESP32 audio to 16-bit mono with shift
- Saves audio to rolling file sink
- Forwards audio to backend
"""

import os
import argparse
import asyncio
import logging
import pathlib
from typing import Optional
import random

import numpy as np
import httpx
from wyoming.audio import AudioChunk

from easy_audio_interfaces import RollingFileSink
from easy_audio_interfaces.network.network_interfaces import TCPServer, SocketClient
from wyoming.client import AsyncClient

DEFAULT_PORT = 8989
SAMP_RATE = 16000
CHANNELS = 1
SAMP_WIDTH = 2  # bytes (16-bit)
RECONNECT_DELAY = 5  # seconds

# Authentication configuration 
# The below two are deliberately different so that someone who wants to skip auth with simple-backend can do so
BACKEND_URL = "http://host.docker.internal:8000"  # Backend API URL
BACKEND_WS_URL = "ws://host.docker.internal:8000"  # Backend WebSocket URL
AUTH_USERNAME = os.getenv("AUTH_USERNAME")  # Can be email or 6-character user_id
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD")
DEVICE_NAME = "havpe"  # Device name for client ID generation

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def exponential_backoff_sleep(attempt: int, base_delay: float = 1.0, max_delay: float = 30.0) -> None:
    """
    Sleep with exponential backoff and jitter.
    
    Args:
        attempt: Current attempt number (0-based)
        base_delay: Base delay in seconds
        max_delay: Maximum delay in seconds
    """
    delay = min(base_delay * (2 ** attempt), max_delay)
    # Add jitter to prevent thundering herd
    jitter = random.uniform(0.1, 0.3) * delay
    total_delay = delay + jitter
    logger.debug(f"⏳ Waiting {total_delay:.1f}s before retry (attempt {attempt + 1})")
    await asyncio.sleep(total_delay)


async def get_jwt_token(username: str, password: str, backend_url: str) -> Optional[str]:
    """
    Get JWT token from backend using username and password.
    
    Args:
        username: User email/username
        password: User password
        backend_url: Backend API URL
        
    Returns:
        JWT token string or None if authentication failed
    """
    try:
        logger.info(f"🔐 Authenticating with backend as: {username}")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{backend_url}/auth/jwt/login",
                data={'username': username, 'password': password},
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
        
        if response.status_code == 200:
            auth_data = response.json()
            token = auth_data.get('access_token')
            
            if token:
                logger.info("✅ JWT authentication successful")
                return token
            else:
                logger.error("❌ No access token in response")
                return None
        else:
            error_msg = "Invalid credentials"
            try:
                error_data = response.json()
                error_msg = error_data.get('detail', error_msg)
            except:
                pass
            logger.error(f"❌ Authentication failed: {error_msg}")
            return None
            
    except httpx.TimeoutException:
        logger.error("❌ Authentication request timed out")
        return None
    except httpx.RequestError as e:
        logger.error(f"❌ Authentication request failed: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected authentication error: {e}")
        return None


def create_authenticated_websocket_uri(base_ws_url: str, client_id: str, jwt_token: str) -> str:
    """
    Create WebSocket URI with JWT authentication.
    
    Args:
        base_ws_url: Base WebSocket URL (e.g., "ws://localhost:8000")
        client_id: Client ID for the connection (not used in URL anymore)
        jwt_token: JWT token for authentication
        
    Returns:
        Authenticated WebSocket URI
    """
    return f"{base_ws_url}/ws?codec=pcm&token={jwt_token}&device_name={DEVICE_NAME}"


async def get_authenticated_socket_client(
    backend_url: str, 
    backend_ws_url: str, 
    username: str, 
    password: str
) -> Optional[SocketClient]:
    """
    Create an authenticated WebSocket client for the backend.
    
    Args:
        backend_url: Backend API URL for authentication
        backend_ws_url: Backend WebSocket URL
        username: Authentication username (email or user_id)
        password: Authentication password
        
    Returns:
        Authenticated SocketClient or None if authentication failed
    """
    # Get JWT token
    jwt_token = await get_jwt_token(username, password, backend_url)
    if not jwt_token:
        logger.error("Failed to get JWT token, cannot create authenticated WebSocket client")
        return None
    
    # Create authenticated WebSocket URI (client_id will be generated by backend)
    ws_uri = create_authenticated_websocket_uri(backend_ws_url, "", jwt_token)
    logger.info(f"🔗 Creating WebSocket connection to: {backend_ws_url}/ws?codec=pcm&token={jwt_token[:20]}...&device_name={DEVICE_NAME}")
    
    # Create socket client
    return SocketClient(uri=ws_uri)


class ESP32TCPServer(TCPServer):
    """
    A TCP server for ESP32 devices streaming 32-bit stereo audio.

    Handles the specific format used by ESPHome voice_assistant component:
    - 32-bit little-endian samples (S32_LE)
    - 2 channels (stereo, left/right interleaved)
    - 16kHz sample rate
    - Channel 0 (left) contains processed voice
    - Channel 1 (right) is unused/muted

    The server extracts the left channel and converts from 32-bit to 16-bit
    following the official Home Assistant approach.
    """

    def __init__(self, *args, **kwargs):
        # Set default parameters for ESP32 Voice Kit
        kwargs.setdefault("sample_rate", 16000)
        kwargs.setdefault("channels", 2)
        kwargs.setdefault("sample_width", 4)  # 32-bit = 4 bytes
        super().__init__(*args, **kwargs)

    async def read(self) -> Optional[AudioChunk]:
        """
        Read audio data from the ESP32 TCP client.

        Converts 32-bit stereo data to 16-bit mono by:
        1. Reading raw 32-bit little-endian data
        2. Reshaping to stereo pairs
        3. Extracting left channel (channel 0)
        4. Converting from 32-bit to 16-bit by right-shifting 16 bits

        Returns:
            AudioChunk with 16-bit mono audio, or None if no data/connection closed
        """
        # Get the raw audio chunk from the parent class
        chunk = await super().read()
        if chunk is None:
            return None

        raw_data = chunk.audio

        # Handle empty data
        if len(raw_data) == 0:
            return None

        # Ensure we have complete 32-bit samples (multiple of 8 bytes for stereo)
        if len(raw_data) % 8 != 0:
            logger.warning(
                f"Received incomplete audio frame: {len(raw_data)} bytes, truncating to nearest complete frame"
            )
            raw_data = raw_data[: len(raw_data) - (len(raw_data) % 8)]

        try:
            # Official Home Assistant approach:
            # 1. Parse as 32-bit little-endian integers
            pcm32 = np.frombuffer(raw_data, dtype="<i4")  # 32-bit little-endian

            # 2. Reshape to stereo pairs and extract left channel (channel 0)
            pcm32 = pcm32.reshape(-1, 2)[:, 0]  # Take LEFT channel only

            # 3. Convert from 32-bit to 16-bit by dropping padding and lower bits
            pcm16 = (pcm32 >> 16).astype(np.int16)  # Right shift 16 bits

            # Convert back to bytes
            audio_bytes = pcm16.tobytes()

            return AudioChunk(
                audio=audio_bytes,
                rate=self._sample_rate,
                channels=1,  # Output is mono (left channel only)
                width=2,  # 16-bit = 2 bytes
            )

        except Exception as e:
            logger.error(f"Error processing ESP32 audio data: {e}")
            return None


async def ensure_socket_connection(socket_client: SocketClient) -> bool:
    """Ensure socket client is connected, with exponential backoff retry logic."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info(f"Attempting to connect to authenticated WebSocket (attempt {attempt + 1}/{max_retries})...")
            await socket_client.open()
            logger.info("✅ Authenticated WebSocket connection established")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to connect to WebSocket: {e}")
            if attempt < max_retries - 1:
                await exponential_backoff_sleep(attempt)
            else:
                logger.error("❌ All WebSocket connection attempts failed")
                return False
    return False


async def create_and_connect_socket_client() -> Optional[SocketClient]:
    """Create a new authenticated socket client and connect it."""
    if not AUTH_USERNAME:
        logger.error("❌ AUTH_USERNAME is required for authentication")
        return None
    
    socket_client = await get_authenticated_socket_client(
        backend_url=BACKEND_URL,
        backend_ws_url=BACKEND_WS_URL,
        username=str(AUTH_USERNAME),
        password=str(AUTH_PASSWORD)
    )
    
    if not socket_client:
        logger.error("❌ Failed to create authenticated socket client")
        return None
    
    # Try to connect
    if await ensure_socket_connection(socket_client):
        return socket_client
    else:
        logger.error("❌ Failed to establish connection with new socket client")
        return None


async def send_with_retry(socket_client: SocketClient, chunk: AudioChunk) -> tuple[bool, bool]:
    """
    Send chunk with retry logic.
    
    Returns:
        Tuple of (success, needs_reconnect)
        - success: True if chunk was sent successfully
        - needs_reconnect: True if we should create a new authenticated client
    """
    max_retries = 2
    for attempt in range(max_retries):
        try:
            await socket_client.write(chunk)
            return True, False  # Success, no reconnect needed
        except Exception as e:
            error_str = str(e).lower()
            
            # Check for authentication-related errors
            if any(auth_err in error_str for auth_err in ['401', 'unauthorized', 'forbidden', 'authentication']):
                logger.warning(f"❌ Authentication error detected: {e}")
                return False, True  # Failed, needs new auth token
            
            logger.warning(f"⚠️ Failed to send chunk (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                if await ensure_socket_connection(socket_client):
                    continue  # Try again with reconnected client
                else:
                    logger.warning("🔄 Connection failed, will need fresh authentication")
                    return False, True  # Connection failed, try new auth
            else:
                logger.error("❌ Failed to send chunk after all retries")
                return False, True  # Failed after retries, try new auth
    
    return False, True





async def process_esp32_audio(
    esp32_server: ESP32TCPServer, 
    socket_client: Optional[SocketClient] = None,
    asr_client: Optional[AsyncClient] = None,
    file_sink: Optional[RollingFileSink] = None
):
    """Process audio chunks from ESP32 server, save to file sink and send to authenticated backend."""
    if (not socket_client) and (not asr_client):
        raise ValueError("Either socket_client or asr_client must be provided")

    try:
        import time
        start_time = time.time()
        logger.info("🎵 Starting to process ESP32 audio with authentication...")
        chunk_count = 0
        failed_sends = 0
        auth_failures = 0
        successful_sends = 0
        
        async for chunk in esp32_server:
            chunk_count += 1
            
            # Health logging every 1000 chunks (~30 seconds at 16kHz)
            if chunk_count % 1000 == 0:
                elapsed = time.time() - start_time
                uptime_mins = elapsed / 60
                success_rate = (successful_sends / chunk_count * 100) if chunk_count > 0 else 0
                logger.info(f"💓 Health: {uptime_mins:.1f}min uptime, {chunk_count} chunks, "
                          f"{success_rate:.1f}% success, {auth_failures} auth failures")
            
            if chunk_count % 100 == 1:  # Log every 100th chunk to reduce spam
                logger.debug(
                    f"📦 Processed {chunk_count} chunks from ESP32, current chunk size: {len(chunk.audio)} bytes"
                )

            # Write to rolling file sink
            if file_sink:
                try:
                    await file_sink.write(chunk)
                except Exception as e:
                    logger.warning(f"⚠️ Failed to write to file sink: {e}")

            # Send to authenticated backend
            if socket_client:
                success, needs_reconnect = await send_with_retry(socket_client, chunk)
                
                if success:
                    successful_sends += 1
                    failed_sends = 0
                    auth_failures = 0
                elif needs_reconnect:
                    auth_failures += 1
                    logger.warning(f"🔄 Need to re-authenticate (failure #{auth_failures})")
                    
                    # Create new authenticated client
                    new_socket_client = await create_and_connect_socket_client()
                    if new_socket_client:
                        socket_client = new_socket_client
                        logger.info("✅ Successfully re-authenticated and reconnected")
                        auth_failures = 0
                        
                        # Retry sending this chunk with new client
                        retry_success, _ = await send_with_retry(socket_client, chunk)
                        if retry_success:
                            successful_sends += 1
                            logger.debug("✅ Chunk sent successfully after re-authentication")
                        else:
                            logger.warning("⚠️ Failed to send chunk even after re-authentication")
                    else:
                        logger.error("❌ Failed to re-authenticate, will retry on next chunk")
                        if auth_failures > 5:
                            logger.error("❌ Too many authentication failures, stopping audio processor")
                            break
                else:
                    failed_sends += 1
                    if failed_sends > 20:
                        logger.error("❌ Too many consecutive send failures, stopping audio processor")
                        break

            # Send to ASR (if implemented)
            # await asr_client.write_event(chunk.event())
            
    except asyncio.CancelledError:
        logger.info("🛑 ESP32 audio processor cancelled")
        raise
    except Exception as e:
        logger.error(f"❌ Error in ESP32 audio processor: {e}")
        raise


async def run_audio_processor(args, esp32_file_sink):
    """Run the audio processor with authentication and reconnect logic."""
    retry_attempt = 0
    while True:
        try:
            # Create ESP32 TCP server with automatic I²S swap detection
            esp32_server = ESP32TCPServer(
                host=args.host,
                port=args.port,
                sample_rate=SAMP_RATE,
                channels=CHANNELS,
                sample_width=4,
            )

            # Create authenticated WebSocket client for sending audio to backend
            logger.info(f"🔐 Setting up authenticated connection to backend...")
            logger.info(f"📡 Backend API: {BACKEND_URL}")
            logger.info(f"🌐 Backend WebSocket: {BACKEND_WS_URL}")
            logger.info(f"👤 Auth Username: {AUTH_USERNAME}")
            logger.info(f"🔧 Device: {DEVICE_NAME}")
            
            socket_client = await create_and_connect_socket_client()
            if not socket_client:
                logger.error("❌ Failed to create authenticated WebSocket client, retrying...")
                await exponential_backoff_sleep(retry_attempt)
                retry_attempt += 1
                continue

            # Reset retry counter on successful connection
            retry_attempt = 0
            logger.info("💚 Authenticated connection established successfully!")

            # Start ESP32 server
            async with esp32_server:
                logger.info(f"🎧 ESP32 server listening on {args.host}:{args.port}")
                logger.info("🎵 Starting authenticated audio recording and processing...")

                # Start audio processing task
                await process_esp32_audio(
                    esp32_server,
                    socket_client,
                    asr_client=None,
                    file_sink=esp32_file_sink
                )
                logger.info("🏁 Audio processing session ended")

        except KeyboardInterrupt:
            logger.info("🛑 Interrupted – stopping")
            break
        except Exception as e:
            logger.error(f"❌ Audio processor error: {e}")
            logger.info(f"🔄 Restarting with exponential backoff...")
            await exponential_backoff_sleep(retry_attempt)
            retry_attempt += 1


async def main():
    # Override global constants with command line arguments
    global BACKEND_URL, BACKEND_WS_URL, AUTH_USERNAME, AUTH_PASSWORD
    
    parser = argparse.ArgumentParser(description="TCP WAV recorder with ESP32 I²S swap detection and backend authentication")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="TCP port to listen on for ESP32 (default 8989)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host address to bind to (default 0.0.0.0)",
    )
    parser.add_argument(
        "--segment-duration",
        type=int,
        default=5,
        help="Duration of each audio segment in seconds (default 5)",
    )
    parser.add_argument(
        "--username",
        type=str,
        default=AUTH_USERNAME,
        help="Backend authentication username (email or 6-character user_id)",
    )
    parser.add_argument(
        "--password",
        type=str,
        default=AUTH_PASSWORD,
        help="Backend authentication password",
    )
    parser.add_argument(
        "--backend-url",
        type=str,
        default=BACKEND_URL,
        help=f"Backend API URL (default: {BACKEND_URL})",
    )
    parser.add_argument(
        "--backend-ws-url",
        type=str,
        default=BACKEND_WS_URL,
        help=f"Backend WebSocket URL (default: {BACKEND_WS_URL})",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v: INFO, -vv: DEBUG")
    parser.add_argument("--debug-audio", action="store_true", help="Debug audio recording")
    args = parser.parse_args()
    
    BACKEND_URL = args.backend_url
    BACKEND_WS_URL = args.backend_ws_url
    AUTH_USERNAME = args.username
    AUTH_PASSWORD = args.password

    loglevel = logging.WARNING - (10 * min(args.verbose, 2))
    logging.basicConfig(format="%(asctime)s  %(levelname)s  %(message)s", level=loglevel)
    
    # Print startup banner with authentication info
    logger.info("🎵 ========================================")
    logger.info("🎵 Chronicle HAVPE Relay with Authentication")
    logger.info("🎵 ========================================")
    logger.info(f"🎧 ESP32 Server: {args.host}:{args.port}")
    logger.info(f"📡 Backend API: {BACKEND_URL}")
    logger.info(f"🌐 Backend WebSocket: {BACKEND_WS_URL}")
    logger.info(f"👤 Auth Username: {AUTH_USERNAME}")
    logger.info(f"🔧 Device: {DEVICE_NAME}")
    logger.info(f"🔧 Debug Audio: {'Enabled' if args.debug_audio else 'Disabled'}")
    logger.info("🎵 ========================================")
    
    # Test authentication on startup
    logger.info("🔐 Testing backend authentication...")
    try:
        if not AUTH_USERNAME or not AUTH_PASSWORD:
            logger.error("❌ Missing authentication credentials")
            logger.error("💡 Set AUTH_USERNAME and AUTH_PASSWORD environment variables or use command line arguments")
            return
        test_token = await get_jwt_token(AUTH_USERNAME, AUTH_PASSWORD, BACKEND_URL)
        if test_token:
            logger.info("✅ Authentication test successful! Ready to start.")
        else:
            logger.error("❌ Authentication test failed! Please check credentials.")
            logger.error("💡 Update AUTH_USERNAME and AUTH_PASSWORD constants or use command line arguments")
            return
    except Exception as e:
        logger.error(f"❌ Authentication test error: {e}")
        logger.error("💡 Make sure the backend is running and accessible")
        return

    # Create recordings directory
    recordings = pathlib.Path("audio_chunks")
    recordings.mkdir(exist_ok=True)

    if args.debug_audio:
        esp32_recordings = recordings / "esp32_raw"
        esp32_recordings.mkdir(exist_ok=True, parents=True)


    # Create rolling file sink for ESP32 data
    if args.debug_audio:
        logger.info("Debug audio recording enabled")
        esp32_file_sink = RollingFileSink(
            directory=esp32_recordings,
            prefix="esp32_raw",
            segment_duration_seconds=args.segment_duration,
            sample_rate=SAMP_RATE,
            channels=CHANNELS,
            sample_width=SAMP_WIDTH,
        )
        await esp32_file_sink.open()
    else:
        logger.info("Debug audio recording disabled")
        esp32_file_sink = None

    try:
        await run_audio_processor(args, esp32_file_sink)
    except KeyboardInterrupt:
        logger.info("Interrupted – shutting down")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        logger.info("Recording session ended")


if __name__ == "__main__":
    asyncio.run(main())
