"""Local wearable client — background service that auto-scans, connects,
and streams audio from OMI/Neo devices to the Chronicle backend.

CLI usage:
    ./start.sh              # Menu bar mode (default)
    ./start.sh run          # Headless mode (for launchd)
    ./start.sh menu         # Menu bar mode
    ./start.sh scan         # One-shot scan, print nearby devices
    ./start.sh install      # Install launchd agent
    ./start.sh uninstall    # Remove launchd agent
    ./start.sh status       # Show service status
    ./start.sh logs         # Tail log file
"""

import argparse
import asyncio
import logging
import os
from typing import Any

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

    decoder = OmiOpusDecoder()
    loop = asyncio.get_running_loop()

    # Raw BLE data queue — written from BLE thread via call_soon_threadsafe
    ble_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1000)
    # Backend Opus queue — written from BLE callback via call_soon_threadsafe
    backend_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=500)

    def _enqueue_ble(data: bytes) -> None:
        # Push raw BLE data to local processing queue
        try:
            ble_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("BLE queue full, dropping frame")
        # Push Opus payload directly to backend (decoupled from local file I/O)
        if backend_enabled and len(data) > 3:
            try:
                backend_queue.put_nowait(data[3:])
            except asyncio.QueueFull:
                logger.warning("Backend queue full, dropping frame")

    def handle_ble_data(_sender: Any, data: bytes) -> None:
        try:
            loop.call_soon_threadsafe(_enqueue_ble, data)
        except RuntimeError:
            pass  # event loop closed

    def handle_button_event(_sender: Any, data: bytes) -> None:
        try:
            state = parse_button_event(data)
        except Exception as e:
            logger.error("Button event parse error: %s", e)
            return
        if state != ButtonState.IDLE:
            logger.info("Button event: %s", state.name)
            asyncio.run_coroutine_threadsafe(send_button_event(state.name), loop)

    device_name = device["name"] or device["type"]
    conn = create_connection(device["mac"], device["type"])

    file_sink = RollingFileSink(
        directory="./audio_chunks",
        prefix=f"{device_name}_audio",
        segment_duration_seconds=30,
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )

    async def process_audio() -> None:
        """Decode BLE data -> PCM for local file sink."""
        while True:
            data = await ble_queue.get()
            decoded_pcm = decoder.decode_packet(data)
            if decoded_pcm:
                chunk = AudioChunk(audio=decoded_pcm, rate=16000, width=2, channels=1)
                await file_sink.write(chunk)

    async def backend_stream_wrapper() -> None:
        async def queue_to_stream():
            while True:
                raw_opus = await backend_queue.get()
                if raw_opus is None:
                    break
                yield raw_opus

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

                worker_tasks = [
                    asyncio.create_task(process_audio(), name="process_audio"),
                ]
                if backend_enabled:
                    worker_tasks.append(asyncio.create_task(backend_stream_wrapper(), name="backend_stream"))

                disconnect_task = asyncio.create_task(
                    conn.wait_until_disconnected(), name="disconnect"
                )

                logger.info("Streaming audio from %s [%s]%s", device_name, device["mac"],
                            "" if backend_enabled else " (local-only, backend disabled)")

                # Wait for disconnect or any worker to fail
                all_tasks = [disconnect_task] + worker_tasks
                done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)

                # Cancel remaining tasks and wait for cleanup
                for task in pending:
                    task.cancel()
                for task in pending:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Re-raise if a worker failed (not just disconnect)
                for task in done:
                    if task is not disconnect_task and task.exception():
                        raise task.exception()
        except Exception as e:
            logger.error("Error during device session: %s", e, exc_info=True)
        finally:
            await backend_queue.put(None)


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


async def scan_and_print() -> None:
    """One-shot scan: print a table of nearby devices and exit."""
    config = load_config()
    known = {d["mac"]: d for d in config.get("devices", [])}
    auto_discover = config.get("auto_discover", True)

    print("Scanning for BLE wearable devices (5s)...\n")
    discovered = await BleakScanner.discover(timeout=5.0)

    devices = []
    for d in discovered:
        if d.address in known:
            entry = known[d.address]
            devices.append({
                "mac": d.address,
                "name": entry.get("name", d.name or "Unknown"),
                "type": entry.get("type", detect_device_type(d.name or "")),
                "rssi": d.rssi,
                "known": True,
            })
        elif auto_discover and d.name:
            lower = d.name.casefold()
            if "omi" in lower or "neo" in lower or "friend" in lower:
                devices.append({
                    "mac": d.address,
                    "name": d.name,
                    "type": detect_device_type(d.name),
                    "rssi": d.rssi,
                    "known": False,
                })

    if not devices:
        print("No wearable devices found.")
        return

    devices.sort(key=lambda x: x.get("rssi", -999), reverse=True)

    # Print table
    print(f"{'Name':<20} {'MAC':<20} {'Type':<8} {'RSSI':<8} {'Known'}")
    print("-" * 70)
    for d in devices:
        print(f"{d['name']:<20} {d['mac']:<20} {d['type']:<8} {d['rssi']:<8} {'yes' if d['known'] else 'auto'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle-wearable",
        description="Chronicle local wearable client — connect BLE devices and stream audio.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("menu", help="Launch menu bar app (default)")
    sub.add_parser("run", help="Headless mode — scan, connect, and stream (for launchd)")
    sub.add_parser("scan", help="One-shot scan — print nearby devices and exit")
    sub.add_parser("install", help="Install macOS launchd agent (auto-start on login)")
    sub.add_parser("uninstall", help="Remove macOS launchd agent")
    sub.add_parser("status", help="Show launchd service status")
    sub.add_parser("logs", help="Tail service log file")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "menu"  # Default to menu mode

    if command == "run":
        asyncio.run(run())

    elif command == "menu":
        from menu_app import run_menu_app
        run_menu_app()

    elif command == "scan":
        asyncio.run(scan_and_print())

    elif command == "install":
        from service import install
        install()

    elif command == "uninstall":
        from service import uninstall
        uninstall()

    elif command == "status":
        from service import status
        status()

    elif command == "logs":
        from service import logs
        logs()


if __name__ == "__main__":
    main()
