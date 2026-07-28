"""macOS menu bar app for the local wearable client.

Provides a system tray icon with device scanning, connection management,
and status display. Runs BLE operations in a background asyncio thread
(see chronicle_wearable.ble — shared with the unified Chronicle tray).
"""

import datetime as _dt
import logging
import os
import subprocess
from collections import deque
from typing import Optional

import rumps
from chronicle_wearable.ble import AsyncioThread, BLEManager, SharedState
from dotenv import load_dotenv, set_key
from chronicle_wearable.cli import ENV_PATH
from chronicle_wearable.screen_capture import (
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
    # Lazy import: macOS-only (AppKit/Foundation, not available cross-platform)
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
            [
                "open",
                f"x-apple.systempreferences:com.apple.preference.security?{anchor}",
            ],
            check=False,
        )
    except Exception as e:
        logger.warning("Failed to open privacy pane %s: %s", anchor, e)


def _show_settings_dialog(capture, env_path: str) -> bool:
    """Show the capture-settings form. On Save, validates, applies changes live to
    the running ``capture`` manager, and persists them to ``env_path`` (.env) so
    they survive a restart. Returns True if saved, False if cancelled/invalid."""
    # Lazy import: macOS-only (AppKit/Foundation, not available cross-platform)
    from AppKit import NSAlert, NSButton, NSSwitchButton, NSTextField, NSView
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
    # Lazy import: macOS-only (AppKit, not available cross-platform)
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
