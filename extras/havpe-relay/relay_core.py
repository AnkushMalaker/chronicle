"""
Shared relay logic for HAVPE → Chronicle WebSocket bridge.

Provides the core Wyoming protocol forwarding functions used by both the CLI
relay (main.py) and the macOS menu bar relay (menu_relay.py).
"""

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import websockets
from device_controller import DeviceController
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


@dataclass
class RelayConfig:
    """Connection parameters for the relay."""

    backend_url: str
    backend_ws_url: str
    auth_username: str
    auth_password: str
    device_name: str
    esphome_device_ip: str = ""

    @classmethod
    def from_env(cls) -> "RelayConfig":
        import sys
        from pathlib import Path

        # discovery.py lives at the repo root (two levels up)
        _repo_root = str(Path(__file__).resolve().parent.parent.parent)
        if _repo_root not in sys.path:
            sys.path.insert(0, _repo_root)

        try:
            from discovery import resolve_backend_url

            backend_url = resolve_backend_url(os.getenv("BACKEND_URL"), logger=logger)
        except ImportError:
            logger.warning("discovery module unavailable; set BACKEND_URL in .env")
            backend_url = os.getenv("BACKEND_URL") or "http://localhost:8000"

        backend_ws_url = os.getenv("BACKEND_WS_URL") or backend_url.replace(
            "http://", "ws://"
        ).replace("https://", "wss://")

        return cls(
            backend_url=backend_url,
            backend_ws_url=backend_ws_url,
            auth_username=os.getenv("AUTH_USERNAME", ""),
            auth_password=os.getenv("AUTH_PASSWORD", ""),
            device_name=os.getenv("DEVICE_NAME", "havpe"),
            esphome_device_ip=os.getenv("ESPHOME_DEVICE_IP", ""),
        )


async def get_jwt_token(username: str, password: str, backend_url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{backend_url}/auth/jwt/login",
                data={"username": username, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code == 200:
            token = resp.json().get("access_token")
            if token:
                logger.info("Auth OK")
                return token
        logger.error("Auth failed: %d", resp.status_code)
    except Exception as e:
        logger.error("Auth error: %s", e)
    return None


async def forward_tcp_to_ws(
    reader: asyncio.StreamReader,
    ws,
    ws_lock: asyncio.Lock,
    *,
    on_audio_chunk: Callable[[bytes, int], None] | None = None,
    on_audio_event: Callable[[str, dict], None] | None = None,
) -> None:
    """Forward Wyoming messages from device TCP to backend WebSocket.

    Args:
        on_audio_chunk: Called with (payload, payload_length) for each audio-chunk.
        on_audio_event: Called with (msg_type, header) for non-audio-chunk messages
                        (e.g. audio-start, audio-stop).
    """
    while True:
        line = await reader.readline()
        if not line:
            break

        line_str = line.decode().strip()
        if not line_str:
            continue

        try:
            header = json.loads(line_str)
        except json.JSONDecodeError:
            logger.warning(
                "TCP→WS: non-JSON line (stream desynchronized) — ending. "
                "Raw data: %s",
                repr(line_str[:120]),
            )
            break

        payload_length = header.get("payload_length", 0)
        payload: bytes | None = None

        try:
            async with ws_lock:
                await ws.send(line_str)
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)
                    await ws.send(payload)
        except asyncio.IncompleteReadError:
            logger.info("TCP→WS: device disconnected mid-payload — ending")
            break

        msg_type = header.get("type", "")

        if msg_type == "audio-chunk":
            if on_audio_chunk and payload is not None:
                on_audio_chunk(payload, payload_length)
        else:
            logger.info("TCP→WS: %s", msg_type)
            if on_audio_event:
                on_audio_event(msg_type, header)


async def handle_backend_messages(ws, device: DeviceController) -> None:
    """Process messages from backend WebSocket, dispatch to device."""
    async for raw in ws:
        if isinstance(raw, bytes):
            logger.debug("Backend binary message (%d bytes), discarded", len(raw))
            continue

        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug("Backend non-JSON message, discarded: %s", str(raw)[:80])
            continue

        msg_type = msg.get("type", "")
        data = msg.get("data", {})

        if msg_type == "play-audio":
            url = data.get("url", "")
            announcement = data.get("announcement", True)
            logger.info("Backend→device: play-audio %s", url)
            await device.play_audio(url, announcement=announcement)

        elif msg_type == "led-control":
            r = float(data.get("r", 0))
            g = float(data.get("g", 0))
            b = float(data.get("b", 0))
            brightness = float(data.get("brightness", 0.3))
            duration = float(data.get("duration", 5.0))
            logger.info(
                "Backend→device: led-control rgb=(%.1f,%.1f,%.1f) br=%.1f dur=%.1fs",
                r,
                g,
                b,
                brightness,
                duration,
            )
            await device.set_led(r, g, b, brightness, duration=duration)

        else:
            logger.debug("Backend→relay (ignored): %s", msg_type or str(raw)[:80])


async def forward_esphome_events(
    device: DeviceController,
    ws,
    ws_lock: asyncio.Lock,
) -> None:
    """Forward button/dial events from ESPHome API to backend WebSocket."""
    while True:
        event = await device.get_event()
        event_type = event.pop("type")

        wyoming_msg = json.dumps(
            {
                "type": event_type,
                "data": event,
                "payload_length": 0,
            }
        )
        async with ws_lock:
            await ws.send(wyoming_msg)
        logger.info("ESPHome→WS: %s %s", event_type, event)


async def run_device_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    config: RelayConfig,
    *,
    on_audio_chunk: Callable[[bytes, int], None] | None = None,
    on_audio_event: Callable[[str, dict], None] | None = None,
    on_session_start: Callable[[str], None] | None = None,
    on_session_end: Callable[[], None] | None = None,
    on_auth_failure: Callable[[], None] | None = None,
) -> None:
    """Run a single device session: authenticate, connect WS, bridge traffic.

    Args:
        on_audio_chunk: Forwarded to forward_tcp_to_ws.
        on_audio_event: Forwarded to forward_tcp_to_ws.
        on_session_start: Called with the device address string on connect.
        on_session_end: Called when session tears down (always, via finally).
        on_auth_failure: Called if JWT auth fails.
    """
    addr = writer.get_extra_info("peername")
    addr_str = f"{addr[0]}:{addr[1]}" if addr else "unknown"
    device_ip = addr[0] if addr else "127.0.0.1"
    logger.info("Device connected from %s", addr_str)

    if on_session_start:
        on_session_start(addr_str)

    token = await get_jwt_token(
        config.auth_username, config.auth_password, config.backend_url
    )
    if not token:
        logger.error("Auth failed, dropping connection")
        if on_auth_failure:
            on_auth_failure()
        writer.close()
        return

    backend_uri = (
        f"{config.backend_ws_url}/ws?codec=pcm&token={token}"
        f"&device_name={config.device_name}"
    )

    device = DeviceController()
    tasks: list[asyncio.Task] = []

    try:
        async with websockets.connect(backend_uri) as ws:
            logger.info("Backend WS connected, starting bidirectional bridge")

            esphome_ip = config.esphome_device_ip or device_ip
            try:
                api_ok = await asyncio.wait_for(device.connect(esphome_ip), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "ESPHome API connect to %s timed out (5s) — audio-only mode",
                    esphome_ip,
                )
                api_ok = False
            if api_ok:
                logger.info(
                    "ESPHome API connected at %s — button/dial/LED/speaker enabled",
                    esphome_ip,
                )
            else:
                logger.info(
                    "ESPHome API unavailable at %s — audio-only mode", esphome_ip
                )

            ws_lock = asyncio.Lock()
            tasks = [
                asyncio.create_task(
                    forward_tcp_to_ws(
                        reader,
                        ws,
                        ws_lock,
                        on_audio_chunk=on_audio_chunk,
                        on_audio_event=on_audio_event,
                    ),
                    name="tcp→ws",
                ),
                asyncio.create_task(
                    handle_backend_messages(ws, device),
                    name="ws→device",
                ),
            ]
            if api_ok:
                tasks.append(
                    asyncio.create_task(
                        forward_esphome_events(device, ws, ws_lock),
                        name="esphome→ws",
                    )
                )

            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            for t in done:
                if t.exception():
                    logger.error("Task %s failed: %s", t.get_name(), t.exception())
                else:
                    logger.info("Task %s finished", t.get_name())

            for t in pending:
                t.cancel()

    except asyncio.IncompleteReadError:
        logger.info("Device disconnected (incomplete read)")
    except websockets.ConnectionClosed as e:
        logger.info("Backend WS closed: %s", e)
    except Exception as e:
        logger.error("Session error: %s", e)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await device.disconnect()
        writer.close()
        if on_session_end:
            on_session_end()
        logger.info("Session ended")
