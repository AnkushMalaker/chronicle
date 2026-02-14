"""Local wearable client — background service that auto-scans, connects,
and streams audio from OMI/Neo devices to the Chronicle backend."""

import asyncio
import logging
import os
from asyncio import Queue
from typing import Any, AsyncGenerator

import yaml
from bleak import BleakScanner
from dotenv import load_dotenv
from easy_audio_interfaces.filesystem import RollingFileSink
from friend_lite import ButtonState, Neo1Connection, OmiConnection, WearableConnection, parse_button_event
from friend_lite.decoder import OmiOpusDecoder
from wyoming.audio import AudioChunk

from backend_sender import send_button_event, stream_to_backend

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "devices.yml")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def check_config() -> bool:
    """Check that required configuration is present. Returns True if backend streaming is possible."""
    if not os.path.exists(ENV_PATH):
        logger.warning("No .env file found — copy .env.template to .env and fill in your settings")
        logger.warning("Audio will be saved locally but NOT streamed to the backend")
        return False

    missing = []
    if not os.getenv("ADMIN_EMAIL"):
        missing.append("ADMIN_EMAIL")
    if not os.getenv("ADMIN_PASSWORD"):
        missing.append("ADMIN_PASSWORD")
    if not os.getenv("BACKEND_HOST"):
        missing.append("BACKEND_HOST")

    if missing:
        logger.warning("Missing environment variables: %s", ", ".join(missing))
        logger.warning("Audio will be saved locally but NOT streamed to the backend")
        return False

    return True


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def detect_device_type(name: str) -> str:
    """Infer device type from BLE advertised name."""
    lower = name.casefold()
    if "neo" in lower:
        return "neo1"
    return "omi"


def create_connection(mac: str, device_type: str) -> WearableConnection:
    """Factory: returns the right connection class based on device type."""
    if device_type == "neo1":
        return Neo1Connection(mac)
    return OmiConnection(mac)


async def scan_for_device(config: dict):
    """Scan BLE and return the first matching known or auto-discovered device.

    Returns a dict with keys: mac, name, type  — or None.
    """
    known = {d["mac"]: d for d in config.get("devices", [])}
    auto_discover = config.get("auto_discover", True)

    logger.info("Scanning for wearable devices...")
    discovered = await BleakScanner.discover(timeout=5.0)

    # Check known devices first
    for d in discovered:
        if d.address in known:
            entry = known[d.address]
            logger.info("Found known device: %s [%s]", entry.get("name", d.name), d.address)
            return {
                "mac": d.address,
                "name": entry.get("name", d.name),
                "type": entry.get("type", detect_device_type(d.name or "")),
            }

    # Auto-discover any recognised OMI/Neo device
    if auto_discover:
        for d in discovered:
            if d.name and ("omi" in d.name.casefold() or "neo" in d.name.casefold() or "friend" in d.name.casefold()):
                dtype = detect_device_type(d.name)
                logger.info("Auto-discovered %s device: %s [%s]", dtype, d.name, d.address)
                return {"mac": d.address, "name": d.name, "type": dtype}

    return None


async def connect_and_stream(device: dict, backend_enabled: bool = True) -> None:
    """Connect to a device, subscribe to audio (and buttons for OMI),
    and stream to the Chronicle backend until disconnected."""

    audio_queue: Queue[bytes] = Queue()
    decoder = OmiOpusDecoder()

    def handle_ble_data(_sender: Any, data: bytes) -> None:
        decoded_pcm = decoder.decode_packet(data)
        if decoded_pcm:
            try:
                audio_queue.put_nowait(decoded_pcm)
            except Exception as e:
                logger.error("Queue error: %s", e)

    def handle_button_event(_sender: Any, data: bytes) -> None:
        try:
            state = parse_button_event(data)
        except Exception as e:
            logger.error("Button event parse error: %s", e)
            return
        if state != ButtonState.IDLE:
            logger.info("Button event: %s", state.name)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(send_button_event(state.name))
            except RuntimeError:
                logger.debug("No running event loop, cannot send button event")

    device_name = device["name"] or device["type"]
    conn = create_connection(device["mac"], device["type"])

    # Cap at 500 chunks (~15s of audio) so a dead backend doesn't eat memory
    backend_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=500)

    file_sink = RollingFileSink(
        directory="./audio_chunks",
        prefix=f"{device_name}_audio",
        segment_duration_seconds=30,
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )

    async def process_audio() -> None:
        async for chunk_bytes in source_bytes(audio_queue):
            chunk = AudioChunk(audio=chunk_bytes, rate=16000, width=2, channels=1)
            await file_sink.write(chunk)
            if backend_enabled:
                try:
                    backend_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass  # backend not keeping up, drop chunk — file is saved

    async def backend_stream_wrapper() -> None:
        async def queue_to_stream():
            while True:
                chunk = await backend_queue.get()
                if chunk is None:
                    break
                yield chunk

        try:
            await stream_to_backend(queue_to_stream(), device_name=device_name)
        except Exception as e:
            logger.error("Backend streaming error: %s", e, exc_info=True)

    async with file_sink:
        try:
            async with conn:
                await conn.subscribe_audio(handle_ble_data)

                # Device-specific setup
                if isinstance(conn, OmiConnection):
                    await conn.subscribe_button(handle_button_event)
                elif isinstance(conn, Neo1Connection):
                    logger.info("Waking Neo1 device...")
                    await conn.wake()

                tasks = [
                    conn.wait_until_disconnected(),
                    process_audio(),
                ]
                if backend_enabled:
                    tasks.append(backend_stream_wrapper())

                logger.info("Streaming audio from %s [%s]%s", device_name, device["mac"],
                            "" if backend_enabled else " (local-only, backend disabled)")
                await asyncio.gather(*tasks)
        except Exception as e:
            logger.error("Error during device session: %s", e, exc_info=True)
        finally:
            await backend_queue.put(None)


async def source_bytes(queue: Queue[bytes]) -> AsyncGenerator[bytes, None]:
    while True:
        chunk = await queue.get()
        try:
            yield chunk
        finally:
            queue.task_done()


async def run() -> None:
    config = load_config()
    scan_interval = config.get("scan_interval", 10)
    backend_enabled = check_config()

    logger.info("Local wearable client started — scanning for devices...")

    while True:
        device = await scan_for_device(config)
        if device:
            logger.info("Connecting to %s [%s] (type=%s)", device["name"], device["mac"], device["type"])
            await connect_and_stream(device, backend_enabled=backend_enabled)
            logger.info("Device disconnected, resuming scan...")
        else:
            logger.debug("No devices found, retrying in %ds...", scan_interval)
            await asyncio.sleep(scan_interval)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
