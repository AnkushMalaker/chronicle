"""Backend streaming module — sends audio to Chronicle via Wyoming WebSocket protocol."""

import asyncio
import json
import logging
import os
import ssl
import sys
from collections.abc import Callable
from typing import AsyncGenerator
from urllib.parse import quote

import websockets
from chronicle_client import ClientConfig
from chronicle_client.voice_session import (
    ServerUpgradeRequired,
    WearableVoiceProtocolError,
    WearableVoiceSession,
)

from .output_route import HostOutputPolicy, MacOutputRouteDetector, resolve_host_output
from .playback import AfplayPlaybackTarget, ElatoPlaybackTarget

logger = logging.getLogger(__name__)

# Backend address, credential and device name all come from the shared client
# config (repository-root .env), so this module no longer derives any of them.
_config = ClientConfig.from_env()
VERIFY_SSL = _config.verify_ssl
# Use the tray host as a route-verified v1 output for speakerless devices.
PLAY_BACKEND_AUDIO = os.getenv("PLAY_BACKEND_AUDIO", "true").lower() == "true"


backend_url = _config.backend_url
USE_HTTPS = backend_url.startswith("https")
websocket_uri = f"{_config.backend_ws_url}/ws?codec=opus"
CHRONICLE_API_KEY = _config.api_key
logger.info("Wearable backend resolved: %s (ws: %s)", backend_url, websocket_uri)


# Module-level websocket reference for sending control messages (e.g., button events)
_active_websocket = None
_active_send_lock: asyncio.Lock | None = None


async def send_button_event(button_state: str) -> None:
    """Send a button event to the backend via the active WebSocket connection."""
    if _active_websocket is None or _active_send_lock is None:
        logger.debug("No active websocket, dropping button event: %s", button_state)
        return

    event = {
        "type": "button-event",
        "data": {"state": button_state},
        "payload_length": None,
    }
    async with _active_send_lock:
        await _active_websocket.send(json.dumps(event) + "\n")
    logger.info("Sent button event to backend: %s", button_state)


async def receive_handler(
    websocket, logger, voice_session: WearableVoiceSession
) -> None:
    """Background task to receive messages from backend.

    Processes pongs (keepalive), interim transcripts, and other messages.
    Critical for WebSocket stability.

    Protocol-v1 owns interactive response playback. A binary frame is accepted
    only after a fully bound ``response.audio`` header.
    """
    try:
        while True:
            message = await websocket.recv()
            if isinstance(message, (bytes, bytearray)):
                await voice_session.handle_binary(bytes(message))
                continue
            try:
                data = json.loads(message)
                msg_type = data.get("type", "unknown")
                if await voice_session.handle_event(data):
                    continue
                if msg_type == "interim_transcript":
                    text = data.get("data", {}).get("text", "")[:50]
                    is_final = data.get("data", {}).get("is_final", False)
                    logger.debug(
                        "Interim transcript (%s): %s...",
                        "FINAL" if is_final else "partial",
                        text,
                    )
                elif msg_type == "ready":
                    logger.info("Backend ready message: %s", data.get("message"))
                elif msg_type == "error" and data.get("error") in {
                    "client_upgrade_required",
                    "server_upgrade_required",
                }:
                    logger.error("Voice protocol upgrade required: %s", data)
                else:
                    logger.debug("Received message type: %s", msg_type)
            except json.JSONDecodeError:
                logger.debug("Received non-JSON message: %s", str(message)[:50])
            except WearableVoiceProtocolError as error:
                logger.error("Voice protocol violation: %s", error)
                raise
    except websockets.exceptions.ConnectionClosed:
        logger.info("Backend connection closed")
    except asyncio.CancelledError:
        logger.info("Receive handler cancelled")
        raise
    except Exception as e:
        logger.error("Receive handler error: %s", e, exc_info=True)
        await websocket.close(code=1002, reason="voice protocol error")
        raise
    finally:
        await voice_session.close()


# Reconnect backoff tuning for the backend WebSocket. Mirrors the BLE-side
# backoff in menu_app.py: capped exponential backoff, reset to the initial
# value once a connection has stayed healthy for MIN_HEALTHY_DURATION.
_BACKOFF_INITIAL = 1.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_MAX = 30.0
_MIN_HEALTHY_DURATION = 30.0


async def _monitor_host_output(route_detector, initial_route, on_change) -> None:
    """End a capture epoch when the host output route changes."""

    while True:
        await asyncio.sleep(1.0)
        observed = await route_detector.detect()
        if observed != initial_route:
            await on_change(observed)
            return


async def stream_to_backend(
    stream: AsyncGenerator[bytes, None],
    device_name: str = "wearable",
    speaker=None,
    output_policy: HostOutputPolicy | str = HostOutputPolicy.AUTO,
    on_voice_status: Callable[[str], None] | None = None,
    initial_capture_epoch: int = 0,
    on_capture_epoch: Callable[[int], None] | None = None,
) -> None:
    """Stream raw Opus audio to backend using Wyoming protocol with JWT authentication.

    Persistent-retry semantics: a transient backend-WebSocket drop never ends the
    session. On a WS/connection error we re-fetch the JWT (mid-session refresh) and
    re-dial with capped exponential backoff, continuing to consume the same audio
    generator. The session only ends when the generator is exhausted (the caller's
    queue_to_stream yields None at true session end), which breaks the reconnect loop.

    Audio produced during a reconnect gap is dropped: the generator only yields live
    chunks pulled from the queue, so chunks pulled while no WS is connected are simply
    sent on the next successful dial or lost — matching the pre-existing outage
    behavior (no buffering across outages).
    """
    ssl_context = None
    if USE_HTTPS:
        ssl_context = ssl.create_default_context()
        if not VERIFY_SSL:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

    global _active_send_lock, _active_websocket

    policy = HostOutputPolicy(output_policy)
    status = on_voice_status or (lambda _status: None)

    backoff = _BACKOFF_INITIAL
    session_ended = False
    chunk_count = 0
    device_playback_target = None
    if speaker is not None:
        candidate = ElatoPlaybackTarget(speaker, on_status=status)
        try:
            await candidate.prepare()
            device_playback_target = candidate
            status("Elato speaker · ready")
            logger.info("Elato speaker protocol v1 is available")
        except RuntimeError as error:
            logger.warning("Elato interactive speaker disabled: %s", error)
    capture_allowed = asyncio.Event()
    capture_allowed.set()
    route_detector = MacOutputRouteDetector()
    capture_epoch = initial_capture_epoch
    interactive_disabled = False

    while not session_ended:
        # The API key does not expire, so a re-dial needs no token refresh.
        uri_with_token = (
            f"{websocket_uri}&token={CHRONICLE_API_KEY}"
            f"&device_name={quote(device_name)}"
        )

        connected_at = None
        try:
            logger.info("Connecting to WebSocket: %s", websocket_uri)
            async with websockets.connect(
                uri_with_token,
                ssl=ssl_context,
                ping_interval=20,
                ping_timeout=120,
                close_timeout=10,
            ) as websocket:
                connected_at = asyncio.get_running_loop().time()
                _active_websocket = websocket
                _active_send_lock = asyncio.Lock()
                playback_target = None
                route_monitor_task = None
                if not interactive_disabled:
                    playback_target = device_playback_target
                if (
                    playback_target is None
                    and not interactive_disabled
                    and PLAY_BACKEND_AUDIO
                    and sys.platform == "darwin"
                ):
                    route = await route_detector.detect()
                    selection = resolve_host_output(policy, route)
                    status(selection.status)
                    if selection.enabled:

                        async def route_changed(_observed) -> None:
                            status(f"OMI mic → {route.name} · route changed")
                            asyncio.get_running_loop().call_later(
                                0.05,
                                lambda: asyncio.create_task(
                                    websocket.close(
                                        code=1012,
                                        reason="audio output route changed",
                                    )
                                ),
                            )

                        playback_target = AfplayPlaybackTarget(
                            selection=selection,
                            route=route,
                            route_detector=route_detector,
                            capture_allowed=capture_allowed,
                            on_status=status,
                            on_route_change=route_changed,
                        )
                        route_monitor_task = asyncio.create_task(
                            _monitor_host_output(route_detector, route, route_changed),
                            name="mac-output-route-monitor",
                        )
                        logger.info("Host voice output: %s", selection.status)
                if playback_target is not None:
                    capture_epoch += 1
                    if on_capture_epoch is not None:
                        on_capture_epoch(capture_epoch)
                voice_session = WearableVoiceSession(
                    websocket,
                    capture_epoch=capture_epoch,
                    playback_target=playback_target,
                    send_lock=_active_send_lock,
                )

                ready_msg = await websocket.recv()
                logger.info("Backend ready: %s", ready_msg)

                receive_task = asyncio.create_task(
                    receive_handler(websocket, logger, voice_session)
                )

                try:
                    audio_start = {
                        "type": "audio-start",
                        "data": voice_session.audio_start_data(),
                        "payload_length": None,
                    }
                    async with _active_send_lock:
                        await websocket.send(json.dumps(audio_start) + "\n")
                    logger.info("Sent audio-start event")
                    await voice_session.wait_until_ready()

                    # Reset backoff once we've been streaming long enough to call
                    # this connection healthy (mirrors MIN_HEALTHY_DURATION).
                    async for opus_data in stream:
                        chunk_count += 1

                        if (
                            backoff != _BACKOFF_INITIAL
                            and connected_at is not None
                            and asyncio.get_running_loop().time() - connected_at
                            >= _MIN_HEALTHY_DURATION
                        ):
                            backoff = _BACKOFF_INITIAL

                        if not capture_allowed.is_set():
                            continue

                        audio_chunk_header = {
                            "type": "audio-chunk",
                            "data": {
                                "rate": 16000,
                                "width": 2,
                                "channels": 1,
                            },
                            "payload_length": len(opus_data),
                        }
                        async with _active_send_lock:
                            await websocket.send(json.dumps(audio_chunk_header) + "\n")
                            await websocket.send(opus_data)

                        if chunk_count % 100 == 0:
                            logger.info("Sent %d chunks", chunk_count)

                    # Generator exhausted (queue_to_stream yielded None) → true
                    # session end. Send audio-stop and break the reconnect loop.
                    session_ended = True
                    audio_stop = {
                        "type": "audio-stop",
                        "data": {},
                        "payload_length": None,
                    }
                    async with _active_send_lock:
                        await websocket.send(json.dumps(audio_stop) + "\n")
                    logger.info("Sent audio-stop event. Total chunks: %d", chunk_count)

                finally:
                    _active_websocket = None
                    _active_send_lock = None
                    if route_monitor_task is not None:
                        route_monitor_task.cancel()
                        await asyncio.gather(route_monitor_task, return_exceptions=True)
                    receive_task.cancel()
                    try:
                        await receive_task
                    except asyncio.CancelledError:
                        logger.info("Receive task cancelled successfully")

        except ServerUpgradeRequired as error:
            _active_websocket = None
            _active_send_lock = None
            interactive_disabled = True
            status("Voice output unavailable · backend upgrade required")
            logger.error("%s; reconnecting as capture-only", error)
        except (websockets.exceptions.WebSocketException, OSError) as e:
            # Transient WS/connection drop while audio is still flowing → reconnect
            # without ending the BLE session. Re-dial with capped backoff.
            _active_websocket = None
            if session_ended:
                break
            healthy = (
                connected_at is not None
                and asyncio.get_running_loop().time() - connected_at
                >= _MIN_HEALTHY_DURATION
            )
            if healthy:
                backoff = _BACKOFF_INITIAL
            logger.warning("Backend WS dropped (%s); reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
