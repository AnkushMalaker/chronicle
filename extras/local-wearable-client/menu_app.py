"""macOS menu bar app for the local wearable client.

Provides a system tray icon with device scanning, connection management,
and status display. Runs BLE operations in a background asyncio thread.
"""

import asyncio
import datetime as _dt
import logging
import os
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import rumps
import yaml
from bleak import BleakScanner
from dotenv import load_dotenv
from main import (
    CONFIG_PATH,
    ENV_PATH,
    check_config,
    connect_and_stream,
    detect_device_type,
    load_config,
)
from screen_capture import (
    ScreenCaptureManager,
    accessibility_ok,
    request_permissions,
    screen_recording_ok,
)

logger = logging.getLogger(__name__)

# Explicit path (not CWD-relative) so settings persisted by the Capture Settings
# form are reloaded even when launched under launchd with a different CWD.
load_dotenv(ENV_PATH)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _app_version() -> str:
    """A short identifier of the running source — git short-hash + commit date,
    so you can confirm at a glance which build the menu bar app is running.
    Falls back to this file's modification time when git isn't available."""
    try:
        out = subprocess.check_output(
            [
                "git",
                "-C",
                _HERE,
                "log",
                "-1",
                "--format=%h (%cd)",
                "--date=format:%Y-%m-%d %H:%M",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    try:
        mtime = _dt.datetime.fromtimestamp(os.path.getmtime(__file__))
        return f"src {mtime:%Y-%m-%d %H:%M}"
    except Exception:
        return "unknown"


class MemoryLogHandler(logging.Handler):
    """Keep recent formatted log lines in memory for display in the menu bar UI."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


log_buffer = MemoryLogHandler()


def _show_logs_dialog(title: str, lines) -> None:
    """Show log lines in a scrollable modal dialog."""
    from AppKit import (
        NSAlert,
        NSBezelBorder,
        NSFont,
        NSScrollView,
        NSTextView,
        NSViewWidthSizable,
    )
    from Foundation import NSMakeRect, NSMakeSize

    text = "\n".join(lines) or "(no logs yet)"
    width, height = 720, 380

    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    scroll.setBorderType_(NSBezelBorder)
    scroll.setAutohidesScrollers_(False)

    content = scroll.contentSize()
    text_view = NSTextView.alloc().initWithFrame_(
        NSMakeRect(0, 0, content.width, content.height)
    )
    text_view.setMinSize_(NSMakeSize(0, content.height))
    text_view.setMaxSize_(NSMakeSize(1e7, 1e7))
    text_view.setVerticallyResizable_(True)
    text_view.setHorizontallyResizable_(False)
    text_view.setAutoresizingMask_(NSViewWidthSizable)
    text_view.setEditable_(False)
    text_view.setFont_(NSFont.userFixedPitchFontOfSize_(11))
    text_view.textContainer().setContainerSize_(NSMakeSize(content.width, 1e7))
    text_view.textContainer().setWidthTracksTextView_(True)
    text_view.setString_(text)
    text_view.scrollRangeToVisible_((len(text), 0))

    scroll.setDocumentView_(text_view)

    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(f"Last {len(lines)} log line(s)")
    alert.addButtonWithTitle_("Close")
    alert.setAccessoryView_(scroll)
    alert.runModal()


# Capture-settings form: (attr on manager, .env key, label, kind). For booleans,
# ``invert`` means the .env key stores the negated value (CAPTURE_NO_DEDUP).
_SETTINGS_FIELDS = [
    ("save_scale", "CAPTURE_SCALE", "Save resolution (0.05–1.0):", "float", False),
    (
        "skip_idle_secs",
        "CAPTURE_SKIP_IDLE_SECS",
        "Skip while idle ≥ (sec, 0=off):",
        "float",
        False,
    ),
    (
        "retention_days",
        "CAPTURE_RETENTION_DAYS",
        "Delete after (days, 0=keep):",
        "int",
        False,
    ),
    ("thumb_max", "CAPTURE_THUMB_MAX", "Dedup hash thumbnail (px):", "int", False),
    ("dedup", "CAPTURE_NO_DEDUP", "Dedup identical frames", "bool", True),
    ("ocr", "CAPTURE_OCR", "Run OCR on each frame (CPU-heavy)", "bool", False),
    (
        "compact_every_mins",
        "CAPTURE_COMPACT_EVERY_MINS",
        "Compact to video every (min; 0=off, not recommended):",
        "int",
        False,
    ),
    (
        "compact_quality",
        "CAPTURE_COMPACT_QUALITY",
        "Video quality (0–100):",
        "int",
        False,
    ),
]


def _open_captures_dir(path) -> None:
    """Reveal the captures directory in Finder (creating it if missing)."""
    try:
        os.makedirs(path, exist_ok=True)
        subprocess.run(["open", str(path)], check=False)
    except Exception as e:
        logger.warning("Failed to open captures dir %s: %s", path, e)


def _open_privacy_pane(anchor: str) -> None:
    """Open a System Settings → Privacy & Security pane (e.g. ``Privacy_ScreenCapture``).

    macOS does not let an app revoke its own TCC grant programmatically, so
    "revoking" means sending the user to the pane where they can toggle it off.
    """
    try:
        subprocess.run(
            ["open", f"x-apple.systempreferences:com.apple.preference.security?{anchor}"],
            check=False,
        )
    except Exception as e:
        logger.warning("Failed to open privacy pane %s: %s", anchor, e)


def _show_settings_dialog(capture, env_path: str) -> bool:
    """Show the capture-settings form. On Save, validates, applies changes live to
    the running ``capture`` manager, and persists them to ``env_path`` (.env) so
    they survive a restart. Returns True if saved, False if cancelled/invalid."""
    from AppKit import NSAlert, NSButton, NSSwitchButton, NSTextField, NSView
    from dotenv import set_key
    from Foundation import NSMakeRect

    row_h, label_w, field_w, pad = 30, 230, 110, 10
    width = label_w + field_w + 3 * pad
    height = len(_SETTINGS_FIELDS) * row_h

    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    widgets = {}
    for i, (attr, _key, label, kind, _inv) in enumerate(_SETTINGS_FIELDS):
        # Rows top-to-bottom (AppKit origin is bottom-left).
        y = height - (i + 1) * row_h
        current = getattr(capture, attr)
        if kind == "bool":
            box = NSButton.alloc().initWithFrame_(
                NSMakeRect(pad, y, width - 2 * pad, row_h - 6)
            )
            box.setButtonType_(NSSwitchButton)
            box.setTitle_(label)
            box.setState_(1 if current else 0)
            view.addSubview_(box)
            widgets[attr] = box
        else:
            lbl = NSTextField.alloc().initWithFrame_(
                NSMakeRect(pad, y, label_w, row_h - 6)
            )
            lbl.setStringValue_(label)
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setEditable_(False)
            lbl.setSelectable_(False)
            view.addSubview_(lbl)
            fld = NSTextField.alloc().initWithFrame_(
                NSMakeRect(label_w + 2 * pad, y, field_w, row_h - 6)
            )
            txt = f"{current:g}" if kind == "float" else str(int(current))
            fld.setStringValue_(txt)
            view.addSubview_(fld)
            widgets[attr] = fld

    # One button per capture permission. When a grant is already in place there's
    # nothing left to grant, so it becomes a "Revoke …" button that opens System
    # Settings (macOS won't let an app drop its own TCC grant programmatically).
    rec_granted = screen_recording_ok()
    acc_granted = accessibility_ok()
    rec_title = "Revoke Screen Recording…" if rec_granted else "Grant Screen Recording"
    acc_title = "Revoke Accessibility…" if acc_granted else "Grant Accessibility"

    alert = NSAlert.alloc().init()
    alert.setMessageText_("Capture Settings")
    alert.setInformativeText_("Changes apply immediately and persist across restarts.")
    alert.addButtonWithTitle_("Save")  # 1000
    alert.addButtonWithTitle_("Open Screenshots…")  # 1001
    alert.addButtonWithTitle_(rec_title)  # 1002
    alert.addButtonWithTitle_(acc_title)  # 1003
    alert.addButtonWithTitle_("Cancel")  # 1004
    alert.setAccessoryView_(view)

    # Open/Grant/Revoke are side actions: perform them and re-open the same dialog
    # so any values typed so far are preserved (the view/widgets are reused).
    while True:
        resp = alert.runModal()
        if resp == 1001:  # Open Screenshots…
            _open_captures_dir(capture.capture_dir)
            continue
        if resp == 1002:  # Grant / Revoke Screen Recording
            if rec_granted:
                _open_privacy_pane("Privacy_ScreenCapture")
                rumps.notification(
                    "Chronicle — Screen Recording",
                    "",
                    "Already granted. To revoke, toggle Chronicle off in "
                    "System Settings → Privacy & Security → Screen Recording.",
                )
            else:
                granted = screen_recording_ok(prompt=True)
                rumps.notification(
                    "Chronicle — Screen Recording",
                    "",
                    f"{'Granted' if granted else 'NOT granted'}. Approve in System "
                    "Settings → Privacy & Security if needed, then restart.",
                )
            continue
        if resp == 1003:  # Grant / Revoke Accessibility
            if acc_granted:
                _open_privacy_pane("Privacy_Accessibility")
                rumps.notification(
                    "Chronicle — Accessibility",
                    "",
                    "Already granted. To revoke, toggle Chronicle off in "
                    "System Settings → Privacy & Security → Accessibility.",
                )
            else:
                granted = accessibility_ok(prompt=True)
                rumps.notification(
                    "Chronicle — Accessibility",
                    "",
                    f"{'Granted' if granted else 'NOT granted'}. Approve in System "
                    "Settings → Privacy & Security if needed, then restart.",
                )
            continue
        if resp != 1000:  # NSAlertFirstButtonReturn == Save; anything else cancels
            return False
        break

    # Parse + validate before applying anything.
    parsed = {}
    for attr, _key, label, kind, _inv in _SETTINGS_FIELDS:
        w = widgets[attr]
        if kind == "bool":
            parsed[attr] = bool(w.state())
            continue
        raw = str(w.stringValue()).strip()
        try:
            val = float(raw) if kind == "float" else int(raw)
        except ValueError:
            rumps.notification(
                "Chronicle — Capture Settings",
                "",
                f"Invalid value for “{label.rstrip(':')}”: {raw!r}",
            )
            return False
        parsed[attr] = val

    # Clamp to sane ranges.
    parsed["save_scale"] = max(0.05, min(1.0, parsed["save_scale"]))
    parsed["skip_idle_secs"] = max(0.0, parsed["skip_idle_secs"])
    parsed["retention_days"] = max(0, parsed["retention_days"])
    parsed["thumb_max"] = max(16, parsed["thumb_max"])
    parsed["compact_every_mins"] = max(0, parsed["compact_every_mins"])
    parsed["compact_quality"] = max(0, min(100, parsed["compact_quality"]))

    # Apply live (the capture loop reads these attributes each tick) + persist.
    for attr, key, _label, kind, invert in _SETTINGS_FIELDS:
        val = parsed[attr]
        setattr(capture, attr, val)
        if kind == "bool":
            env_val = "1" if (val if not invert else not val) else "0"
        elif kind == "int":
            env_val = str(int(val))
        else:
            env_val = f"{val:g}"
        try:
            set_key(env_path, key, env_val, quote_mode="never")
        except Exception as e:
            logger.warning("Failed to persist %s to .env: %s", key, e)

    logger.info("Capture settings updated: %s", parsed)
    return True


# --- Shared state -----------------------------------------------------------


@dataclass
class SharedState:
    """Thread-safe state shared between the rumps UI and the asyncio BLE thread."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    status: str = "idle"  # idle | scanning | connecting | connected | error
    connected_device: Optional[dict] = None  # {name, mac, type}
    nearby_devices: list[dict] = field(
        default_factory=list
    )  # [{name, mac, type, rssi}]
    error: Optional[str] = None
    chunks_sent: int = 0
    battery_level: int = -1  # -1 = unknown

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "connected_device": (
                    self.connected_device.copy() if self.connected_device else None
                ),
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
        self._MIN_HEALTHY_DURATION: float = (
            30.0  # connections shorter than this trigger backoff
        )

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
                match = next(
                    (d for d in snap["nearby_devices"] if d["mac"] == self._target_mac),
                    None,
                )
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
                devices.append(
                    {
                        "mac": d.address,
                        "name": entry.get("name", d.name or "Unknown"),
                        "type": entry.get("type", detect_device_type(d.name or "")),
                        "rssi": adv.rssi,
                    }
                )
                continue

            # Auto-discover recognized names
            if auto_discover and d.name:
                lower = d.name.casefold()
                if "omi" in lower or "neo" in lower or "friend" in lower or "elato" in lower:
                    devices.append(
                        {
                            "mac": d.address,
                            "name": d.name,
                            "type": detect_device_type(d.name),
                            "rssi": adv.rssi,
                        }
                    )

        # Sort by signal strength (strongest first)
        devices.sort(key=lambda x: x.get("rssi", -999), reverse=True)

        new_status = (
            "idle" if self.state.snapshot()["status"] != "connected" else "connected"
        )
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
                    self._backoff_seconds = min(
                        self._backoff_seconds * 2, self._BACKOFF_MAX
                    )
                logger.info(
                    "Connection lasted %.1fs (< %.0fs), backoff %.0fs before next attempt",
                    elapsed,
                    self._MIN_HEALTHY_DURATION,
                    self._backoff_seconds,
                )
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

    def __init__(
        self, state: SharedState, ble: BLEManager, capture: ScreenCaptureManager
    ) -> None:
        super().__init__("Chronicle", title="⊙")
        self.state = state
        self.ble = ble
        self.capture = capture

        # Build initial menu
        self.status_item = rumps.MenuItem("Status: Starting...", callback=None)
        self.disconnect_item = rumps.MenuItem("Disconnect", callback=self.on_disconnect)
        self.devices_header = rumps.MenuItem("Nearby Devices:", callback=None)
        self.scan_item = rumps.MenuItem("Scan Now", callback=self.on_scan)
        self.capture_item = rumps.MenuItem(
            "Screen Capture: Off", callback=self.on_toggle_capture
        )
        self.settings_item = rumps.MenuItem(
            "Capture Settings…", callback=self.on_capture_settings
        )
        self.logs_item = rumps.MenuItem("View Logs", callback=self.on_view_logs)
        self.version_item = rumps.MenuItem(f"Version: {_app_version()}", callback=None)

        self.menu = [
            self.status_item,
            self.disconnect_item,
            None,  # separator
            self.devices_header,
            rumps.MenuItem("  (scanning...)", callback=None),
            None,  # separator
            self.scan_item,
            self.capture_item,
            self.settings_item,
            self.logs_item,
            None,  # separator
            self.version_item,
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
            self.status_item.title = (
                f"Connected: {dev['name']} [{dev['mac'][-8:]}]{bat_str}"
            )
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

        # Update screen-capture toggle label
        cap = self.capture.stats.snapshot()
        if cap["running"]:
            suffix = f" ({cap['frames']} frames)" if cap["frames"] else ""
            self.capture_item.title = f"Screen Capture: On{suffix}"
        else:
            self.capture_item.title = "Screen Capture: Off"

    def _rebuild_device_menu(
        self, devices: list[dict], connected: Optional[dict]
    ) -> None:
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

    def on_toggle_capture(self, _sender) -> None:
        """Handle 'Screen Capture' toggle click."""
        running = self.capture.toggle()
        logger.info("User toggled screen capture -> %s", "On" if running else "Off")

    def on_capture_settings(self, _sender) -> None:
        """Open the capture-settings form (resolution, dedup, OCR, retention)."""
        logger.info("User opened capture settings")
        _show_settings_dialog(self.capture, ENV_PATH)

    def on_view_logs(self, _sender) -> None:
        """Show recent log output in a scrollable dialog."""
        _show_logs_dialog("Chronicle Wearable — Logs", list(log_buffer.lines))


# --- Entry point --------------------------------------------------------------


def run_menu_app() -> None:
    """Launch the menu bar app with background BLE thread."""
    # Register as accessory app so macOS allows menu bar icons
    # (non-bundled Python processes default to no-UI policy on Sequoia)
    from AppKit import NSApplication

    NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory

    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(format=log_format, level=logging.INFO)
    log_buffer.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(log_buffer)

    # Request capture permissions up front by default. The TCC prompts only
    # appear if a grant is actually missing (an already-granted check returns
    # silently), so this is a no-op on subsequent launches. A manual re-trigger
    # lives in Capture Settings for when a grant was previously denied.
    request_permissions()

    state = SharedState()
    bg = AsyncioThread()
    bg.start()

    ble = BLEManager(state, bg)
    ble.start_scanning()

    # Screen + accessibility capture (toggle from the menu). Optionally auto-start
    # under the launchd agent: set CAPTURE_AUTOSTART=1, but only begin once Screen
    # Recording is actually granted (otherwise it would log empty frames).
    # CAPTURE_OCR=1 additionally runs Apple Vision OCR on each frame (CPU-heavy).
    # Storage controls (see CAPTURE.md "Storage"):
    #   CAPTURE_NO_DEDUP=1        store every frame even if unchanged
    #   CAPTURE_SKIP_IDLE_SECS=N  skip screenshots while idle >= N s (0 disables)
    #   CAPTURE_RETENTION_DAYS=N  delete screenshots older than N days (0 = keep)
    #   CAPTURE_SCALE=F           save frames at fraction F of native res (default 0.5)
    #   CAPTURE_COMPACT_EVERY_MINS=N  collapse old JPEGs into HEVC every N min (0 = off)
    #   CAPTURE_COMPACT_QUALITY=Q  HEVC quality 0-100 (default 60)
    #   CAPTURE_COMPACT_AFTER_SECS=N  only compact frames older than N s (default 600)
    #   CAPTURE_COMPACT_MIN_BATTERY=P  skip compaction on battery below P% (default 20)
    capture_ocr = os.getenv("CAPTURE_OCR", "").lower() in ("1", "true", "yes")
    capture_dedup = os.getenv("CAPTURE_NO_DEDUP", "").lower() not in (
        "1",
        "true",
        "yes",
    )
    capture = ScreenCaptureManager(
        ocr=capture_ocr,
        dedup=capture_dedup,
        skip_idle_secs=float(os.getenv("CAPTURE_SKIP_IDLE_SECS", "90")),
        retention_days=int(os.getenv("CAPTURE_RETENTION_DAYS", "14")),
        save_scale=float(os.getenv("CAPTURE_SCALE", "0.5")),
        thumb_max=int(os.getenv("CAPTURE_THUMB_MAX", "256")),
        compact_every_mins=int(os.getenv("CAPTURE_COMPACT_EVERY_MINS", "30")),
        compact_after_secs=float(os.getenv("CAPTURE_COMPACT_AFTER_SECS", "600")),
        compact_quality=int(os.getenv("CAPTURE_COMPACT_QUALITY", "60")),
        compact_min_battery=int(os.getenv("CAPTURE_COMPACT_MIN_BATTERY", "20")),
    )
    if os.getenv("CAPTURE_AUTOSTART", "").lower() in ("1", "true", "yes"):
        if screen_recording_ok():
            capture.start()
        else:
            logger.warning(
                "CAPTURE_AUTOSTART set but Screen Recording not granted — "
                "use 'Grant Screen Recording' in Capture Settings, then restart."
            )

    app = WearableMenuApp(state, ble, capture)
    app.run()
