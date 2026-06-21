"""Backend streaming module — sends audio to Chronicle via Wyoming WebSocket protocol."""

import asyncio
import base64
import json
import logging
import os
import ssl
import tempfile
from typing import AsyncGenerator, Optional
from urllib.parse import quote

import httpx
import websockets
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() == "true"
# Play backend TTS ("play-audio") responses on the laptop speaker. The wearable
# has no speaker, so the relay laptop acts as the output device.
PLAY_BACKEND_AUDIO = os.getenv("PLAY_BACKEND_AUDIO", "true").lower() == "true"


async def play_audio_on_laptop(audio_b64: str, fmt: str = "wav") -> None:
    """Decode base64 audio from a backend play-audio frame and play it on the
    laptop speaker via macOS `afplay`. Runs as a non-blocking subprocess so the
    WebSocket keepalive/receive loop is never stalled."""
    try:
        audio = base64.b64decode(audio_b64)
    except Exception as e:
        logger.warning("play-audio: failed to decode base64 audio: %s", e)
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=f".{fmt}", delete=False
        ) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name

        logger.info("play-audio: playing %d bytes on laptop speaker", len(audio))
        proc = await asyncio.create_subprocess_exec(
            "afplay",
            tmp_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            logger.warning("play-audio: afplay exited with code %s", proc.returncode)
    except FileNotFoundError:
        logger.error("play-audio: `afplay` not found (macOS only); cannot play audio")
    except Exception as e:
        logger.error("play-audio: playback failed: %s", e, exc_info=True)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _resolve_backend_url() -> str:
    """Resolve the backend URL, logging how it was chosen (see discovery.py)."""
    host = os.getenv("BACKEND_HOST")
    if host:
        scheme = (
            "https" if os.getenv("USE_HTTPS", "false").lower() == "true" else "http"
        )
        url = f"{scheme}://{host}"
        logger.info("Backend URL from BACKEND_HOST: %s", url)
        return url

    import sys
    from pathlib import Path

    _repo_root = str(Path(__file__).resolve().parent.parent.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    try:
        from discovery import resolve_backend_url

        return resolve_backend_url(None, logger=logger)
    except ImportError:
        logger.warning("discovery module unavailable; set BACKEND_HOST in .env")
        return "http://localhost:8000"


backend_url = _resolve_backend_url()
USE_HTTPS = backend_url.startswith("https")
_host_part = backend_url.split("://", 1)[-1]
websocket_uri = f"{'wss' if USE_HTTPS else 'ws'}://{_host_part}/ws?codec=opus"
logger.info("Wearable backend resolved: %s (ws: %s)", backend_url, websocket_uri)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

# Module-level websocket reference for sending control messages (e.g., button events)
_active_websocket = None


async def send_button_event(button_state: str) -> None:
    """Send a button event to the backend via the active WebSocket connection."""
    if _active_websocket is None:
        logger.debug("No active websocket, dropping button event: %s", button_state)
        return

    event = {
        "type": "button-event",
        "data": {"state": button_state},
        "payload_length": None,
    }
    await _active_websocket.send(json.dumps(event) + "\n")
    logger.info("Sent button event to backend: %s", button_state)


async def get_jwt_token(username: str, password: str) -> Optional[str]:
    """Get JWT token from backend using username and password."""
    try:
        logger.info("Authenticating with backend as: %s", username)

        async with httpx.AsyncClient(timeout=10.0, verify=VERIFY_SSL) as client:
            response = await client.post(
                f"{backend_url}/auth/jwt/login",
                data={"username": username, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code == 200:
            auth_data = response.json()
            token = auth_data.get("access_token")
            if token:
                logger.info("JWT authentication successful")
                return token
            else:
                logger.error("No access token in response")
                return None
        else:
            error_msg = "Invalid credentials"
            try:
                error_data = response.json()
                error_msg = error_data.get("detail", error_msg)
            except Exception:
                pass
            logger.error("Authentication failed: %s", error_msg)
            return None

    except httpx.TimeoutException:
        logger.error("Authentication request timed out")
        return None
    except httpx.RequestError as e:
        logger.error("Authentication request failed: %s", e)
        return None
    except Exception as e:
        logger.error("Unexpected authentication error: %s", e)
        return None


async def receive_handler(websocket, logger) -> None:
    """Background task to receive messages from backend.

    Processes pongs (keepalive), interim transcripts, and other messages.
    Critical for WebSocket stability.
    """
    try:
        while True:
            message = await websocket.recv()
            try:
                data = json.loads(message)
                msg_type = data.get("type", "unknown")
                if msg_type == "interim_transcript":
                    text = data.get("data", {}).get("text", "")[:50]
                    is_final = data.get("data", {}).get("is_final", False)
                    logger.debug(
                        "Interim transcript (%s): %s...",
                        "FINAL" if is_final else "partial",
                        text,
                    )
                elif msg_type == "play-audio":
                    # Backend TTS response — play it on the laptop speaker since
                    # the wearable has none. Spawned as a task so playback
                    # doesn't block receiving further frames / keepalive.
                    payload = data.get("data", {})
                    audio_b64 = payload.get("audio_b64", "")
                    if PLAY_BACKEND_AUDIO and audio_b64:
                        asyncio.create_task(
                            play_audio_on_laptop(
                                audio_b64, payload.get("format", "wav")
                            )
                        )
                    elif not audio_b64:
                        logger.debug("play-audio frame without audio_b64; ignoring")
                elif msg_type == "ready":
                    logger.info("Backend ready message: %s", data.get("message"))
                else:
                    logger.debug("Received message type: %s", msg_type)
            except json.JSONDecodeError:
                logger.debug("Received non-JSON message: %s", str(message)[:50])
    except websockets.exceptions.ConnectionClosed:
        logger.info("Backend connection closed")
    except asyncio.CancelledError:
        logger.info("Receive handler cancelled")
        raise
    except Exception as e:
        logger.error("Receive handler error: %s", e, exc_info=True)


async def stream_to_backend(
    stream: AsyncGenerator[bytes, None],
    device_name: str = "wearable",
) -> None:
    """Stream raw Opus audio to backend using Wyoming protocol with JWT authentication."""
    token = await get_jwt_token(ADMIN_EMAIL, ADMIN_PASSWORD)
    if not token:
        logger.error("Failed to get JWT token, cannot stream audio")
        return

    uri_with_token = f"{websocket_uri}&token={token}&device_name={quote(device_name)}"

    ssl_context = None
    if USE_HTTPS:
        ssl_context = ssl.create_default_context()
        if not VERIFY_SSL:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

    global _active_websocket

    logger.info("Connecting to WebSocket: %s", websocket_uri)
    async with websockets.connect(
        uri_with_token,
        ssl=ssl_context,
        ping_interval=20,
        ping_timeout=120,
        close_timeout=10,
    ) as websocket:
        _active_websocket = websocket

        ready_msg = await websocket.recv()
        logger.info("Backend ready: %s", ready_msg)

        receive_task = asyncio.create_task(receive_handler(websocket, logger))

        try:
            audio_start = {
                "type": "audio-start",
                "data": {
                    "rate": 16000,
                    "width": 2,
                    "channels": 1,
                    "mode": "streaming",
                },
                "payload_length": None,
            }
            await websocket.send(json.dumps(audio_start) + "\n")
            logger.info("Sent audio-start event")

            chunk_count = 0
            async for opus_data in stream:
                chunk_count += 1

                audio_chunk_header = {
                    "type": "audio-chunk",
                    "data": {
                        "rate": 16000,
                        "width": 2,
                        "channels": 1,
                    },
                    "payload_length": len(opus_data),
                }
                await websocket.send(json.dumps(audio_chunk_header) + "\n")
                await websocket.send(opus_data)

                if chunk_count % 100 == 0:
                    logger.info("Sent %d chunks", chunk_count)

            audio_stop = {
                "type": "audio-stop",
                "data": {},
                "payload_length": None,
            }
            await websocket.send(json.dumps(audio_stop) + "\n")
            logger.info("Sent audio-stop event. Total chunks: %d", chunk_count)

        finally:
            _active_websocket = None
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                logger.info("Receive task cancelled successfully")
