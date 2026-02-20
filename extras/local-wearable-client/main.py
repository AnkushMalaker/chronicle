"""Local wearable client — background service that auto-scans, connects,
and streams audio from OMI/Neo devices to the Chronicle backend.

CLI usage:
    ./start.sh              # Menu bar mode (default)
    ./start.sh run          # Headless mode (for launchd)
    ./start.sh menu         # Menu bar mode
    ./start.sh scan         # One-shot scan, print nearby devices
    ./start.sh wifi-sync    # Download stored audio via WiFi sync
    ./start.sh install      # Install launchd agent
    ./start.sh uninstall    # Remove launchd agent
    ./start.sh kickstart    # Relaunch after quit
    ./start.sh status       # Show service status
    ./start.sh logs         # Tail log file
"""

import argparse
import asyncio
import logging
import os
import shutil
import socket
import time
from typing import Any, Callable

import yaml
from backend_sender import send_button_event, stream_to_backend
from bleak import BleakScanner
from dotenv import load_dotenv
from easy_audio_interfaces.filesystem import RollingFileSink
from friend_lite import (
    ButtonState,
    Neo1Connection,
    OmiConnection,
    WearableConnection,
    parse_button_event,
)
from friend_lite.decoder import OmiOpusDecoder
from wifi_join import get_current_wifi, join_wifi_ap
from wifi_receiver import WifiAudioReceiver
from wyoming.audio import AudioChunk

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "devices.yml")
CONFIG_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "devices.yml.template")
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
    if not os.path.exists(CONFIG_PATH) and os.path.exists(CONFIG_TEMPLATE_PATH):
        shutil.copy2(CONFIG_TEMPLATE_PATH, CONFIG_PATH)
        logger.info("Created devices.yml from template")
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


async def scan_all_devices(config: dict) -> list[dict]:
    """Scan BLE and return all matching known or auto-discovered devices.

    Returns a list of dicts with keys: mac, name, type, rssi.
    """
    known = {d["mac"]: d for d in config.get("devices", [])}
    auto_discover = config.get("auto_discover", True)

    logger.info("Scanning for wearable devices...")
    discovered = await BleakScanner.discover(timeout=5.0, return_adv=True)

    devices = []
    for d, adv in discovered.values():
        if d.address in known:
            entry = known[d.address]
            devices.append({
                "mac": d.address,
                "name": entry.get("name", d.name or "Unknown"),
                "type": entry.get("type", detect_device_type(d.name or "")),
                "rssi": adv.rssi,
            })
        elif auto_discover and d.name:
            lower = d.name.casefold()
            if "omi" in lower or "neo" in lower or "friend" in lower:
                devices.append({
                    "mac": d.address,
                    "name": d.name,
                    "type": detect_device_type(d.name),
                    "rssi": adv.rssi,
                })

    devices.sort(key=lambda x: x.get("rssi", -999), reverse=True)
    return devices


async def scan_for_device(config: dict):
    """Scan BLE and return the first matching device, or None."""
    devices = await scan_all_devices(config)
    return devices[0] if devices else None


def prompt_device_selection(devices: list[dict]) -> dict | None:
    """Show an interactive numbered list and let the user pick a device."""
    print(f"\nFound {len(devices)} device(s):\n")
    print(f"  {'#':<4} {'Name':<20} {'MAC':<20} {'Type':<8} {'RSSI'}")
    print("  " + "-" * 60)
    for i, d in enumerate(devices, 1):
        print(f"  {i:<4} {d['name']:<20} {d['mac']:<20} {d['type']:<8} {d.get('rssi', '?')}")

    print()
    while True:
        try:
            choice = input("Select device [1]: ").strip()
            if not choice:
                idx = 0
            else:
                idx = int(choice) - 1
            if 0 <= idx < len(devices):
                return devices[idx]
            print(f"  Please enter a number between 1 and {len(devices)}")
        except ValueError:
            print(f"  Please enter a number between 1 and {len(devices)}")
        except (EOFError, KeyboardInterrupt):
            print()
            return None


async def connect_and_stream(
    device: dict,
    backend_enabled: bool = True,
    on_battery_level: Callable[[int], None] | None = None,
) -> None:
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

                # Battery level
                battery = await conn.read_battery_level()
                if battery >= 0:
                    logger.info("Battery level: %d%%", battery)
                    if on_battery_level:
                        on_battery_level(battery)
                last_battery = [battery]  # mutable container for closure

                def _on_battery(level: int) -> None:
                    if level == last_battery[0]:
                        return
                    last_battery[0] = level
                    logger.info("Battery level: %d%%", level)
                    if on_battery_level:
                        on_battery_level(level)

                try:
                    await conn.subscribe_battery(_on_battery)
                except Exception:
                    logger.debug("Battery notifications not supported by this device")

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
                try:
                    done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
                except asyncio.CancelledError:
                    # External cancellation (e.g. user disconnect) — clean up all workers
                    for task in all_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*all_tasks, return_exceptions=True)
                    raise

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


async def wifi_sync(
    target_mac: str | None = None,
    ssid: str = "Friend",
    password: str = "12345678",
    interface: str | None = None,
    output_dir: str = "./wifi_audio",
) -> None:
    """Download stored audio from an OMI device over WiFi sync."""
    from friend_lite.wifi import WifiErrorCode

    config = load_config()

    # --- Find and connect to device via BLE ---
    if target_mac:
        devices = await scan_all_devices(config)
        device = next(
            (d for d in devices if d["mac"].casefold() == target_mac.casefold()),
            None,
        )
        if not device:
            logger.error("Device %s not found", target_mac)
            return
    else:
        devices = await scan_all_devices(config)
        if not devices:
            logger.error("No devices found")
            return
        if len(devices) == 1:
            device = devices[0]
        else:
            device = prompt_device_selection(devices)
            if device is None:
                return

    conn = OmiConnection(device["mac"])
    original_wifi: str | None = None
    output_file = None
    receiver: WifiAudioReceiver | None = None

    try:
        async with conn:
            logger.info("Connected to %s [%s]", device["name"], device["mac"])

            # Check WiFi support
            if not await conn.is_wifi_supported():
                logger.error("Device does not support WiFi sync")
                return

            # Read storage info
            file_size, offset = await conn.get_storage_info()
            logger.info("Storage: %d bytes available, offset %d", file_size, offset)
            if file_size == 0:
                logger.info("No stored audio to download")
                return

            # Remember current WiFi so we can restore it later
            original_wifi = get_current_wifi(interface)
            if original_wifi:
                logger.info("Current WiFi: %s (will restore after sync)", original_wifi)

            # Send WiFi credentials to device
            logger.info("Configuring device WiFi AP (SSID=%s)...", ssid)
            rc = await conn.setup_wifi(ssid, password)
            if rc != WifiErrorCode.SUCCESS:
                error_name = WifiErrorCode(rc).name if rc in WifiErrorCode._value2member_map_ else f"0x{rc:02X}"
                logger.error("WiFi setup failed: %s", error_name)
                return

            # Prepare output
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, f"wifi_sync_{int(time.time())}.raw")
            output_file = open(output_path, "wb")
            bytes_written = [0]

            def on_tcp_data(data: bytes) -> None:
                output_file.write(data)
                bytes_written[0] += len(data)
                # Progress update every ~1MB
                if bytes_written[0] % (1024 * 1024) < len(data):
                    logger.info("Received %d / %d bytes (%.1f%%)",
                                bytes_written[0], file_size,
                                bytes_written[0] / file_size * 100 if file_size else 0)

            # Tell device to start WiFi AP (creates the network)
            logger.info("Starting device WiFi AP...")
            rc = await conn.start_wifi()
            if rc == WifiErrorCode.SESSION_ALREADY_RUNNING:
                logger.info("WiFi AP already running, continuing...")
            elif rc != WifiErrorCode.SUCCESS:
                error_name = WifiErrorCode(rc).name if rc in WifiErrorCode._value2member_map_ else f"0x{rc:02X}"
                logger.error("WiFi start failed: %s", error_name)
                output_file.close()
                return

            # Start TCP server (on all interfaces, before WiFi switch)
            receiver = WifiAudioReceiver(
                host="0.0.0.0", port=12345, on_data=on_tcp_data
            )
            await receiver.start()

            # Wait for AP to stabilize
            logger.info("Waiting for AP to stabilize...")
            await asyncio.sleep(3)

            # Join device WiFi AP
            logger.info("Joining WiFi AP '%s'...", ssid)
            join_wifi_ap(ssid, password, interface)

            # Wait for the device's AP subnet (192.168.1.x)
            local_ip = None
            prompted_manual = False
            for attempt in range(60):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(0.5)
                    s.connect(("192.168.1.1", 80))
                    local_ip = s.getsockname()[0]
                    s.close()
                except Exception:
                    local_ip = None
                if local_ip and local_ip.startswith("192.168.1."):
                    break
                if attempt == 10 and not prompted_manual:
                    prompted_manual = True
                    logger.info(">>> Auto-join may have failed. Please manually join WiFi '%s' (password: %s) <<<", ssid, password)
                elif attempt % 10 == 0:
                    logger.info("Waiting for connection to '%s' AP... (current IP: %s)", ssid, local_ip)
                await asyncio.sleep(1)

            if not local_ip or not local_ip.startswith("192.168.1."):
                logger.error("Failed to get IP on device AP network (got: %s). Is your WiFi connected to '%s'?", local_ip, ssid)
                await receiver.stop()
                output_file.close()
                if original_wifi:
                    join_wifi_ap(original_wifi, "", interface)
                return
            logger.info("Connected to AP network with IP %s", local_ip)

            # Wait for device to connect to our TCP server
            logger.info("Waiting for device TCP connection...")
            try:
                await asyncio.wait_for(receiver.connected.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.error("Device did not connect to TCP server within 30s")
                await receiver.stop()
                output_file.close()
                if original_wifi:
                    join_wifi_ap(original_wifi, "", interface)
                return
            logger.info("Device connected via TCP")

            # Reconnect BLE (WiFi switch may have invalidated service cache)
            logger.info("Reconnecting BLE for storage read command...")
            await conn.disconnect()
            await asyncio.sleep(1)
            await conn.connect()

            # Send BLE read command — MUST be after WiFi+TCP are up,
            # otherwise firmware sees no BLE + no WiFi and aborts transfer
            logger.info("Sending storage read command (offset=%d)...", offset)
            await conn.start_storage_read(file_num=0, offset=offset)

            # Wait for firmware to process the read command before disconnecting
            # (response=False means write is fire-and-forget, need time to transmit)
            await asyncio.sleep(2)

            # Disconnect BLE to free shared radio for WiFi data transfer
            logger.info("Disconnecting BLE (freeing radio for WiFi)...")
            await conn.disconnect()

            # Wait for transfer to complete or user interrupt
            logger.info("Receiving audio data... (Ctrl+C to stop)")
            try:
                await receiver.finished.wait()
            except asyncio.CancelledError:
                pass

            logger.info("Transfer complete: %d bytes written to %s", bytes_written[0], output_path)

            # Reconnect BLE to send cleanup commands
            logger.info("Reconnecting BLE for cleanup...")
            try:
                await conn.connect()
                await conn.stop_wifi()
                logger.info("Device WiFi stopped")
            except Exception as e:
                logger.warning("BLE cleanup failed (non-fatal): %s", e)

    except Exception as e:
        logger.error("WiFi sync error: %s", e, exc_info=True)
    finally:
        if output_file:
            try:
                output_file.close()
            except Exception:
                pass

        # Restore original WiFi
        if original_wifi:
            logger.info("Restoring WiFi to '%s'...", original_wifi)
            join_wifi_ap(original_wifi, "", interface)

        # Clean up TCP server
        if receiver:
            try:
                await receiver.stop()
            except Exception:
                pass


async def run(target_mac: str | None = None) -> None:
    config = load_config()
    scan_interval = config.get("scan_interval", 10)
    backend_enabled = check_config()

    logger.info("Local wearable client started — scanning for devices...")

    while True:
        devices = await scan_all_devices(config)

        device = None
        if target_mac:
            # --device flag: connect to specific MAC
            device = next((d for d in devices if d["mac"].casefold() == target_mac.casefold()), None)
            if not device:
                logger.debug("Target device %s not found, retrying in %ds...", target_mac, scan_interval)
        elif len(devices) == 1:
            device = devices[0]
        elif len(devices) > 1:
            device = prompt_device_selection(devices)
            if device is None:
                logger.info("No device selected, exiting.")
                return

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
    discovered = await BleakScanner.discover(timeout=5.0, return_adv=True)

    devices = []
    for d, adv in discovered.values():
        if d.address in known:
            entry = known[d.address]
            devices.append({
                "mac": d.address,
                "name": entry.get("name", d.name or "Unknown"),
                "type": entry.get("type", detect_device_type(d.name or "")),
                "rssi": adv.rssi,
                "known": True,
            })
        elif auto_discover and d.name:
            lower = d.name.casefold()
            if "omi" in lower or "neo" in lower or "friend" in lower:
                devices.append({
                    "mac": d.address,
                    "name": d.name,
                    "type": detect_device_type(d.name),
                    "rssi": adv.rssi,
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
    run_parser = sub.add_parser("run", help="Headless mode — scan, connect, and stream (for launchd)")
    run_parser.add_argument("--device", metavar="MAC", help="Connect to a specific device by MAC address")
    sub.add_parser("scan", help="One-shot scan — print nearby devices and exit")
    wifi_parser = sub.add_parser("wifi-sync", help="Download stored audio from device via WiFi sync")
    wifi_parser.add_argument("--device", metavar="MAC", help="Connect to a specific device by MAC address")
    wifi_parser.add_argument("--ssid", default="Friend", help="WiFi AP SSID (default: Friend)")
    wifi_parser.add_argument("--password", default="12345678", help="WiFi AP password (default: 12345678)")
    wifi_parser.add_argument("--interface", metavar="IFACE", help="WiFi interface to use (e.g. en1 for USB adapter)")
    wifi_parser.add_argument("--output-dir", default="./wifi_audio", help="Output directory (default: ./wifi_audio)")

    sub.add_parser("install", help="Install macOS launchd agent (auto-start on login)")
    sub.add_parser("uninstall", help="Remove macOS launchd agent")
    sub.add_parser("kickstart", help="Relaunch the menu bar app (after quit)")
    sub.add_parser("status", help="Show launchd service status")
    sub.add_parser("logs", help="Tail service log file")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "menu"  # Default to menu mode

    if command == "wifi-sync":
        asyncio.run(wifi_sync(
            target_mac=getattr(args, "device", None),
            ssid=args.ssid,
            password=args.password,
            interface=args.interface,
            output_dir=args.output_dir,
        ))

    elif command == "run":
        asyncio.run(run(target_mac=getattr(args, "device", None)))

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

    elif command == "kickstart":
        from service import kickstart
        kickstart()

    elif command == "status":
        from service import status
        status()

    elif command == "logs":
        from service import logs
        logs()


if __name__ == "__main__":
    main()
