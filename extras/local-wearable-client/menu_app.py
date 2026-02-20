"""macOS menu bar app for the local wearable client.

Provides a system tray icon with device scanning, connection management,
and status display. Runs BLE operations in a background asyncio thread.
"""

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

import rumps
import yaml
from bleak import BleakScanner
from dotenv import load_dotenv

from backend_sender import stream_to_backend
from main import CONFIG_PATH, check_config, connect_and_stream, create_connection, detect_device_type, load_config

logger = logging.getLogger(__name__)

load_dotenv()


# --- Shared state -----------------------------------------------------------

@dataclass
class SharedState:
    """Thread-safe state shared between the rumps UI and the asyncio BLE thread."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    status: str = "idle"  # idle | scanning | connecting | connected | error
    connected_device: Optional[dict] = None  # {name, mac, type}
    nearby_devices: list[dict] = field(default_factory=list)  # [{name, mac, type, rssi}]
    error: Optional[str] = None
    chunks_sent: int = 0
    battery_level: int = -1  # -1 = unknown

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "connected_device": self.connected_device.copy() if self.connected_device else None,
                "nearby_devices": [d.copy() for d in self.nearby_devices],
                "error": self.error,
                "chunks_sent": self.chunks_sent,
                "battery_level": self.battery_level,
            }

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)


# --- Asyncio background thread ----------------------------------------------

class AsyncioThread:
    """Runs an asyncio event loop in a daemon thread."""

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait until the loop is running
        while self.loop is None or not self.loop.is_running():
            threading.Event().wait(0.01)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coro(self, coro):
        """Schedule a coroutine on the background loop. Returns a concurrent.futures.Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


# --- BLE manager (runs in the asyncio thread) --------------------------------

class BLEManager:
    """Manages BLE scanning and device connections in the background asyncio thread."""

    def __init__(self, state: SharedState, bg: AsyncioThread) -> None:
        self.state = state
        self.bg = bg
        self.config = load_config()
        self.backend_enabled = check_config()
        self._scan_interval = self.config.get("scan_interval", 10)
        self._connecting = False  # Guard against concurrent _connect() calls
        self._running_task: Optional[asyncio.Task] = None

        # Backoff state for connection failures
        self._backoff_seconds: float = 0  # 0 = no backoff active
        self._BACKOFF_INITIAL: float = 10.0
        self._BACKOFF_MAX: float = 300.0  # 5 minutes
        self._MIN_HEALTHY_DURATION: float = 30.0  # connections shorter than this trigger backoff

        # Restore last connected device for auto-connect
        last = self.config.get("last_connected")
        self._target_mac: Optional[str] = last if last else None
        if self._target_mac:
            logger.info("Will auto-connect to last device: %s", self._target_mac)

    def _save_last_connected(self, mac: Optional[str]) -> None:
        """Persist (or clear) the last connected device MAC in devices.yml."""
        try:
            with open(CONFIG_PATH) as f:
                data = yaml.safe_load(f) or {}
            if mac:
                data["last_connected"] = str(mac)
            else:
                data.pop("last_connected", None)
            with open(CONFIG_PATH, "w") as f:
                yaml.dump(data, f, default_flow_style=False)
            logger.info("Saved last_connected: %s", mac)
        except Exception as e:
            logger.error("Failed to save last_connected: %s", e)

    def start_scanning(self) -> None:
        """Begin the scan-connect loop."""
        self.bg.run_coro(self._scan_loop())

    async def _scan_loop(self) -> None:
        """Continuously scan and auto-connect when a target is set."""
        while True:
            try:
                await self._do_scan()
            except Exception as e:
                logger.error("Scan error: %s", e, exc_info=True)
                self.state.update(status="error", error=str(e))

            # If we have a target and not already connecting/connected, try connecting
            if self._target_mac and not self._connecting:
                snap = self.state.snapshot()
                match = next((d for d in snap["nearby_devices"] if d["mac"] == self._target_mac), None)
                if match:
                    await self._connect(match)

            sleep_time = max(self._scan_interval, self._backoff_seconds)
            await asyncio.sleep(sleep_time)

    async def _do_scan(self) -> None:
        """Run a single BLE scan and update shared state."""
        status = self.state.snapshot()["status"]
        if status in ("connected", "connecting"):
            return  # Don't scan while connected or connecting

        self.state.update(status="scanning")
        config = self.config
        known = {d["mac"]: d for d in config.get("devices", [])}
        auto_discover = config.get("auto_discover", True)

        try:
            discovered = await BleakScanner.discover(timeout=5.0, return_adv=True)
        except Exception as e:
            logger.error("BLE scan failed: %s", e)
            self.state.update(status="error", error=f"Scan failed: {e}")
            return

        devices = []
        for d, adv in discovered.values():
            # Check if known device
            if d.address in known:
                entry = known[d.address]
                devices.append({
                    "mac": d.address,
                    "name": entry.get("name", d.name or "Unknown"),
                    "type": entry.get("type", detect_device_type(d.name or "")),
                    "rssi": adv.rssi,
                })
                continue

            # Auto-discover recognized names
            if auto_discover and d.name:
                lower = d.name.casefold()
                if "omi" in lower or "neo" in lower or "friend" in lower:
                    devices.append({
                        "mac": d.address,
                        "name": d.name,
                        "type": detect_device_type(d.name),
                        "rssi": adv.rssi,
                    })

        # Sort by signal strength (strongest first)
        devices.sort(key=lambda x: x.get("rssi", -999), reverse=True)

        new_status = "idle" if self.state.snapshot()["status"] != "connected" else "connected"
        self.state.update(nearby_devices=devices, status=new_status, error=None)
        logger.info("Scan found %d device(s)", len(devices))

    async def _connect(self, device: dict) -> None:
        """Connect to a device and stream audio.

        Creates a dedicated task for the connection so that cancelling it
        (via request_disconnect) does not kill the calling scan loop.
        """
        if self._connecting or self.state.snapshot()["status"] == "connected":
            return  # Already connecting or connected — skip

        self._connecting = True
        self.state.update(status="connecting", error=None)
        logger.info("Connecting to %s [%s]", device["name"], device["mac"])

        # Create a dedicated task so request_disconnect cancels only it
        task = asyncio.create_task(self._run_connection(device), name="ble_connection")
        self._running_task = task

        start_time = asyncio.get_event_loop().time()
        user_cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            logger.info("Connection cancelled by user")
            user_cancelled = True
        except Exception as e:
            logger.error("Connection error: %s", e, exc_info=True)
            self.state.update(status="error", error=str(e))
        finally:
            self._running_task = None
            self._connecting = False
            self.state.update(status="idle", connected_device=None, battery_level=-1)
            logger.info("Disconnected from %s", device["name"])

            # Backoff logic: if connection was very short, it likely failed
            elapsed = asyncio.get_event_loop().time() - start_time
            if user_cancelled:
                # User-initiated disconnect — reset backoff
                self._backoff_seconds = 0
            elif elapsed < self._MIN_HEALTHY_DURATION:
                # Quick failure — apply exponential backoff
                if self._backoff_seconds == 0:
                    self._backoff_seconds = self._BACKOFF_INITIAL
                else:
                    self._backoff_seconds = min(self._backoff_seconds * 2, self._BACKOFF_MAX)
                logger.info("Connection lasted %.1fs (< %.0fs), backoff %.0fs before next attempt",
                            elapsed, self._MIN_HEALTHY_DURATION, self._backoff_seconds)
            else:
                # Healthy connection — reset backoff
                self._backoff_seconds = 0

    async def _run_connection(self, device: dict) -> None:
        """Run the actual device connection. Executed as a dedicated task."""
        self.state.update(status="connected", connected_device=device, battery_level=-1)
        self._save_last_connected(device["mac"])
        await connect_and_stream(
            device,
            backend_enabled=self.backend_enabled,
            on_battery_level=lambda level: self.state.update(battery_level=level),
        )

    def request_connect(self, mac: str) -> None:
        """Request connection to a device (called from UI thread)."""
        self._target_mac = mac
        # Trigger an immediate scan+connect attempt
        self.bg.run_coro(self._immediate_connect(mac))

    async def _immediate_connect(self, mac: str) -> None:
        """Scan once and connect immediately if device is found."""
        if self._connecting:
            logger.debug("Already connecting, skipping immediate connect")
            return
        await self._do_scan()
        snap = self.state.snapshot()
        match = next((d for d in snap["nearby_devices"] if d["mac"] == mac), None)
        if match:
            await self._connect(match)
        else:
            logger.warning("Device %s not found in scan", mac)

    def request_disconnect(self) -> None:
        """Request disconnection (called from UI thread)."""
        self._target_mac = None
        self._backoff_seconds = 0  # Reset backoff on user-initiated disconnect
        self._save_last_connected(None)
        # Cancel the dedicated connection task on the asyncio thread
        task = self._running_task
        if task and self.bg.loop:
            self.bg.loop.call_soon_threadsafe(task.cancel)

    def request_scan(self) -> None:
        """Trigger an immediate scan (called from UI thread)."""
        self.bg.run_coro(self._do_scan())


# --- rumps menu bar app -------------------------------------------------------

class WearableMenuApp(rumps.App):
    """macOS menu bar app for Chronicle wearable client."""

    # Keys used for device-area menu items (to find and remove them)
    _DEVICE_KEY_PREFIX = "_dev_"
    _NO_DEVICES_KEY = "_no_devices"

    def __init__(self, state: SharedState, ble: BLEManager) -> None:
        super().__init__("Chronicle", title="⊙")
        self.state = state
        self.ble = ble

        # Build initial menu
        self.status_item = rumps.MenuItem("Status: Starting...", callback=None)
        self.disconnect_item = rumps.MenuItem("Disconnect", callback=self.on_disconnect)
        self.devices_header = rumps.MenuItem("Nearby Devices:", callback=None)
        self.scan_item = rumps.MenuItem("Scan Now", callback=self.on_scan)

        self.menu = [
            self.status_item,
            self.disconnect_item,
            None,  # separator
            self.devices_header,
            rumps.MenuItem("  (scanning...)", callback=None),
            None,  # separator
            self.scan_item,
            None,  # separator
        ]
        # Disconnect is always clickable — harmless when not connected

        # Track keys of dynamic items so we can remove them
        self._dynamic_keys: list[str] = []

    @rumps.timer(2)
    def refresh_ui(self, _sender) -> None:
        """Periodically refresh menu from shared state."""
        snap = self.state.snapshot()

        # Update title icon
        status = snap["status"]
        if status == "connected":
            self.title = "●"
        elif status == "scanning" or status == "connecting":
            self.title = "⊙"
        elif status == "error":
            self.title = "⊘"
        else:
            self.title = "⊙"

        # Update status text
        if status == "connected" and snap["connected_device"]:
            dev = snap["connected_device"]
            bat = snap["battery_level"]
            bat_str = f" 🔋{bat}%" if bat >= 0 else ""
            self.status_item.title = f"Connected: {dev['name']} [{dev['mac'][-8:]}]{bat_str}"
        elif status == "connecting":
            self.status_item.title = "Connecting..."
        elif status == "scanning":
            self.status_item.title = "Scanning..."
        elif status == "error":
            self.status_item.title = f"Error: {snap['error'] or 'unknown'}"
        else:
            self.status_item.title = "Idle"

        # Update device list
        self._rebuild_device_menu(snap["nearby_devices"], snap["connected_device"])

    def _rebuild_device_menu(self, devices: list[dict], connected: Optional[dict]) -> None:
        """Replace the device submenu items with fresh MenuItem instances."""
        connected_mac = connected["mac"] if connected else None

        # Remove previous dynamic items
        for key in self._dynamic_keys:
            try:
                del self.menu[key]
            except KeyError:
                pass
        self._dynamic_keys.clear()

        if not devices:
            item = rumps.MenuItem("  (no devices found)", callback=None)
            self.menu.insert_after(self.devices_header.title, item)
            self._dynamic_keys.append(item.title)
            return

        # Add device items in reverse order (insert_after pushes down)
        for device in reversed(devices):
            mac = device["mac"]
            suffix = " \u2713" if mac == connected_mac else ""
            label = f"  {device['name']} [{mac[-8:]}]{suffix}"
            item = rumps.MenuItem(label, callback=self._make_device_callback(device))
            self.menu.insert_after(self.devices_header.title, item)
            self._dynamic_keys.append(item.title)

    def _make_device_callback(self, device: dict):
        """Create a click handler for a device menu item."""
        mac = device["mac"]

        def callback(_sender):
            snap = self.state.snapshot()
            if snap["connected_device"] and snap["connected_device"]["mac"] == mac:
                # Already connected — disconnect
                logger.info("User requested disconnect from %s", mac)
                self.ble.request_disconnect()
            else:
                # Connect to this device
                logger.info("User requested connect to %s", mac)
                self.ble.request_connect(mac)

        return callback

    def on_scan(self, _sender) -> None:
        """Handle 'Scan Now' menu click."""
        logger.info("User triggered manual scan")
        self.ble.request_scan()

    def on_disconnect(self, _sender) -> None:
        """Handle 'Disconnect' menu click."""
        logger.info("User requested disconnect")
        self.ble.request_disconnect()


# --- Entry point --------------------------------------------------------------

def run_menu_app() -> None:
    """Launch the menu bar app with background BLE thread."""
    # Register as accessory app so macOS allows menu bar icons
    # (non-bundled Python processes default to no-UI policy on Sequoia)
    from AppKit import NSApplication
    NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )

    state = SharedState()
    bg = AsyncioThread()
    bg.start()

    ble = BLEManager(state, bg)
    ble.start_scanning()

    app = WearableMenuApp(state, ble)
    app.run()
