"""Backend streaming module — sends audio to Chronicle via Wyoming WebSocket protocol."""

import asyncio
import json
import logging
import os
import ssl
from typing import AsyncGenerator, Optional

import httpx
import websockets
from dotenv import load_dotenv

load_dotenv()

BACKEND_HOST = os.getenv("BACKEND_HOST", "localhost:8000")
USE_HTTPS = os.getenv("USE_HTTPS", "false").lower() == "true"
VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() == "true"

ws_protocol = "wss" if USE_HTTPS else "ws"
http_protocol = "https" if USE_HTTPS else "http"

websocket_uri = f"{ws_protocol}://{BACKEND_HOST}/ws?codec=opus"
backend_url = f"{http_protocol}://{BACKEND_HOST}"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

logger = logging.getLogger(__name__)

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
                    logger.debug("Interim transcript (%s): %s...", "FINAL" if is_final else "partial", text)
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

    uri_with_token = f"{websocket_uri}&token={token}&device_name={device_name}"

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
