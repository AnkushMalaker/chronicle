"""
Shared relay logic for HAVPE → Chronicle WebSocket bridge.

Provides the core Wyoming protocol forwarding functions used by both the CLI
relay (main.py) and the macOS menu bar relay (menu_relay.py).
"""

import asyncio
import base64
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import websockets
from device_controller import DeviceController
from dotenv import load_dotenv
from tone_server import serve_audio_bytes

# Max backend→relay WS frame. Bumped above the websockets 1 MiB default so larger
# inline TTS audio payloads (play-audio audio_b64) are not rejected.
_WS_MAX_SIZE = 16 * 1024 * 1024

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
    # If the device sends nothing for this long, treat it as gone and close the
    # backend WS. Without this a silently-dead device (power off, Wi-Fi drop, no
    # TCP FIN) leaves readline() blocked forever and the backend sees an immortal
    # "connected" client. The device streams continuously when alive, so this only
    # trips on a real disconnect.
    device_idle_timeout: float = 300.0

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
            device_idle_timeout=float(os.getenv("DEVICE_IDLE_TIMEOUT", "300")),
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
    idle_timeout: float | None = None,
) -> None:
    """Forward Wyoming messages from device TCP to backend WebSocket.

    Args:
        on_audio_chunk: Called with (payload, payload_length) for each audio-chunk.
        on_audio_event: Called with (msg_type, header) for non-audio-chunk messages
                        (e.g. audio-start, audio-stop).
        idle_timeout: If set, treat the device as gone when it sends nothing for this
                      many seconds and end the session (closing the backend WS) so the
                      backend doesn't keep a zombie "connected" client.
    """
    while True:
        try:
            if idle_timeout is not None:
                line = await asyncio.wait_for(reader.readline(), timeout=idle_timeout)
            else:
                line = await reader.readline()
        except asyncio.TimeoutError:
            logger.warning(
                "TCP→WS: device idle for %.0fs — treating as disconnected, ending session",
                idle_timeout,
            )
            break
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

        # Read the payload from the device first (device-side failure → end
        # session), then forward header+payload to the WS. A WS-send failure
        # (websockets.ConnectionClosed) is NOT caught here: it propagates so the
        # caller can reconnect the backend WS without ending the device session.
        try:
            if payload_length > 0:
                payload = await reader.readexactly(payload_length)
        except asyncio.IncompleteReadError:
            logger.info("TCP→WS: device disconnected mid-payload — ending")
            break

        async with ws_lock:
            await ws.send(line_str)
            if payload is not None:
                await ws.send(payload)

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
            announcement = data.get("announcement", True)
            url = data.get("url", "")
            audio_b64 = data.get("audio_b64", "")
            if audio_b64:
                # Backend-generated audio (e.g. TTS): the device can't reach the
                # backend, so serve the bytes locally on the LAN.
                try:
                    audio = base64.b64decode(audio_b64)
                    url = serve_audio_bytes(audio, ext=data.get("format", "wav"))
                    logger.info(
                        "Backend→device: play-audio (%d bytes) → %s", len(audio), url
                    )
                except Exception as e:
                    logger.warning(
                        "Backend→device: failed to stage play-audio bytes: %s", e
                    )
                    url = ""
            else:
                logger.info("Backend→device: play-audio %s", url)
            if url:
                await device.play_audio(url, announcement=announcement)
            else:
                logger.warning("Backend→device: play-audio with no url/audio_b64")

        elif msg_type == "led-control":
            r = float(data.get("r", 0))
            g = float(data.get("g", 0))
            b = float(data.get("b", 0))
            brightness = float(data.get("brightness", 0.3))
            duration = float(data.get("duration", 5.0))
            effect = data.get("effect")
            if effect:
                # Animated feedback (e.g. wake-event "Listening"/"Thinking" ring).
                logger.info(
                    "Backend→device: led-control effect=%s rgb=(%.1f,%.1f,%.1f) "
                    "br=%.1f dur=%.1fs",
                    effect,
                    r,
                    g,
                    b,
                    brightness,
                    duration,
                )
                await device.set_led_effect(
                    effect, r, g, b, brightness, duration=duration
                )
            else:
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


async def _maintain_esphome_api(
    device: DeviceController,
    device_ip: str,
    *,
    interval: float = 15.0,
) -> None:
    """Keep the ESPHome API connection alive for the life of a device session.

    aioesphomeapi's ReconnectLogic only re-establishes the raw client; it does
    not re-run our entity discovery + state subscription, so instead we poll and
    re-run the full DeviceController.connect() whenever the API is down. This lets
    button/dial/LED/speaker recover mid-session if the initial connect timed out
    or the API later drops, without ever touching the audio path (which runs over
    a separate TCP socket). Cancelled at session teardown.
    """
    while True:
        await asyncio.sleep(interval)
        if device.connected:
            continue
        try:
            ok = await asyncio.wait_for(device.connect(device_ip), timeout=5.0)
        except asyncio.TimeoutError:
            ok = False
        if ok:
            logger.info("ESPHome API (re)connected at %s mid-session", device_ip)


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

    device = DeviceController()
    tasks: list[asyncio.Task] = []

    # ESPHome API connect happens once and is kept alive across backend-WS
    # reconnects (3D handles its own mid-session reconnect). device_ip is the TCP
    # peer; esphome_ip may be overridden via config.
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
        logger.info("ESPHome API unavailable at %s — audio-only mode", esphome_ip)

    # Optional periodic re-connect of the ESPHome API so button/dial/LED/speaker
    # recover mid-session if the initial connect failed or drops (3D).
    api_reconnect_task = asyncio.create_task(
        _maintain_esphome_api(device, esphome_ip), name="esphome-api-keepalive"
    )

    # Backend-WS reconnect backoff. A WS blip must NOT end the device session:
    # we keep reader/writer (and the ESPHome API) alive and re-dial the backend
    # with capped exponential backoff, re-fetching the JWT each time. The loop
    # ends only on true device disconnect (reader EOF / IncompleteReadError) or
    # idle timeout — both surface as forward_tcp_to_ws returning normally.
    _BACKOFF_INITIAL = 1.0
    _BACKOFF_FACTOR = 2.0
    _BACKOFF_MAX = 30.0
    _MIN_HEALTHY_DURATION = 30.0
    backoff = _BACKOFF_INITIAL
    session_ended = False

    try:
        while not session_ended:
            # Re-fetch the JWT on each re-dial (mid-session refresh for long
            # sessions that can outlive the token lifetime).
            token = await get_jwt_token(
                config.auth_username, config.auth_password, config.backend_url
            )
            if not token:
                logger.error("Auth failed on reconnect; retrying in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
                continue

            backend_uri = (
                f"{config.backend_ws_url}/ws?codec=pcm&token={token}"
                f"&device_name={config.device_name}"
            )

            connected_at = None
            tasks = []
            try:
                async with websockets.connect(backend_uri, max_size=_WS_MAX_SIZE) as ws:
                    connected_at = asyncio.get_running_loop().time()
                    logger.info("Backend WS connected, starting bidirectional bridge")

                    ws_lock = asyncio.Lock()
                    tasks = [
                        asyncio.create_task(
                            forward_tcp_to_ws(
                                reader,
                                ws,
                                ws_lock,
                                on_audio_chunk=on_audio_chunk,
                                on_audio_event=on_audio_event,
                                idle_timeout=config.device_idle_timeout,
                            ),
                            name="tcp→ws",
                        ),
                        asyncio.create_task(
                            handle_backend_messages(ws, device),
                            name="ws→device",
                        ),
                    ]
                    if device.connected:
                        tasks.append(
                            asyncio.create_task(
                                forward_esphome_events(device, ws, ws_lock),
                                name="esphome→ws",
                            )
                        )

                    tcp_task = tasks[0]
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )

                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    # forward_tcp_to_ws finishing normally = device EOF / idle
                    # timeout = true session end. Any task raising (e.g. WS
                    # ConnectionClosed) = transient drop → reconnect.
                    drop_exc: BaseException | None = None
                    for t in done:
                        exc = t.exception()
                        if exc is not None:
                            drop_exc = exc
                            logger.warning("Task %s ended with %s", t.get_name(), exc)
                        else:
                            logger.info("Task %s finished", t.get_name())

                    if tcp_task in done and tcp_task.exception() is None:
                        session_ended = True
                        break

                    if drop_exc is not None and not isinstance(
                        drop_exc, websockets.WebSocketException
                    ):
                        # Unexpected error (not a WS drop) — surface it.
                        raise drop_exc

            except (websockets.WebSocketException, OSError) as e:
                # Backend WS connect/stream failed — reconnect, keep device alive.
                logger.warning(
                    "Backend WS dropped (%s); reconnecting in %.0fs", e, backoff
                )

            # Reset backoff if the connection stayed healthy long enough.
            if (
                connected_at is not None
                and asyncio.get_running_loop().time() - connected_at
                >= _MIN_HEALTHY_DURATION
            ):
                backoff = _BACKOFF_INITIAL

            if not session_ended:
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)

    except asyncio.IncompleteReadError:
        logger.info("Device disconnected (incomplete read)")
    except Exception as e:
        logger.error("Session error: %s", e)
    finally:
        api_reconnect_task.cancel()
        for t in tasks:
            t.cancel()
        await asyncio.gather(api_reconnect_task, *tasks, return_exceptions=True)
        await device.disconnect()
        writer.close()
        if on_session_end:
            on_session_end()
        logger.info("Session ended")
