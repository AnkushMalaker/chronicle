"""Pendant section — BLE wearable (OMI/Neo/Friend) scanning + audio streaming.

Uses the chronicle-wearable package's UI-free BLE stack. Only available when
the tray was installed with the ``pendant`` extra (BLE/audio deps) —
data-only client nodes skip that weight.
"""

import importlib.util
import logging

from chronicle_tray.sections import Section
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu

logger = logging.getLogger(__name__)


class PendantSection(Section):
    title = "Pendant"

    def __init__(self) -> None:
        self.state = None
        self.ble = None
        self.status_item = None
        self.voice_status_item = None
        self.devices_menu = None
        self.voice_policy_menu = None
        self.voice_policy_actions = {}
        self.voice_policy_group = None

    def available(self) -> tuple[bool, str]:
        if importlib.util.find_spec("chronicle_wearable") is None:
            return False, "Pendant: chronicle-wearable is not installed"
        if importlib.util.find_spec("bleak") is None:
            return (
                False,
                "Pendant: install the tray with --pendant to stream from a wearable",
            )
        return True, ""

    def build(self, menu: QMenu) -> None:
        # Deferred: pulls in the BLE/audio stack, which is an optional extra.
        from chronicle_wearable.ble import AsyncioThread, BLEManager, SharedState

        self.state = SharedState()
        bg = AsyncioThread()
        bg.start()
        self.ble = BLEManager(self.state, bg)
        self.ble.start_scanning()

        self.status_item = menu.addAction("Pendant: starting…")
        self.status_item.setEnabled(False)
        self.voice_status_item = menu.addAction("Voice: waiting for pendant")
        self.voice_status_item.setEnabled(False)
        self.devices_menu = menu.addMenu("Devices")
        self._add_voice_output_controls(menu)
        menu.addAction("Scan now", self.ble.request_scan)
        menu.addAction("Disconnect pendant", self.ble.request_disconnect)

    def _add_voice_output_controls(self, menu: QMenu) -> None:
        output_menu = menu.addMenu("Voice output")
        self.voice_policy_menu = output_menu
        self.voice_policy_group = QActionGroup(output_menu)
        self.voice_policy_group.setExclusive(True)
        options = (
            ("auto", "Automatic (recommended)"),
            ("require_headphones", "Require headphones"),
            ("always_half_duplex", "Always speaker-safe"),
        )
        for value, label in options:
            action = QAction(label, output_menu)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, selected=value: (
                    self.ble.request_voice_output_policy(selected)
                )
            )
            self.voice_policy_group.addAction(action)
            output_menu.addAction(action)
            self.voice_policy_actions[value] = action

    def refresh(self) -> None:
        if self.ble is None:
            return
        snap = self.state.snapshot()
        self.status_item.setText(f"Pendant: {self._summary(snap)}")
        self.voice_status_item.setText(f"Voice: {snap['voice_output_status']}")
        selected = self.voice_policy_actions.get(snap["voice_output_policy"])
        if selected is not None:
            selected.setChecked(True)
        self._rebuild_devices(snap)

    def _summary(self, snap: dict) -> str:
        status = snap["status"]
        if status == "connected" and snap["connected_device"]:
            device = snap["connected_device"]
            battery = snap["battery_level"]
            suffix = f" · {battery}%" if battery >= 0 else ""
            return f"connected to {device['name']}{suffix}"
        if status == "error":
            return f"error — {snap['error'] or 'unknown'}"
        return status

    def _rebuild_devices(self, snap: dict) -> None:
        connected = snap["connected_device"]
        connected_mac = connected["mac"] if connected else None
        self.devices_menu.clear()
        if not snap["nearby_devices"]:
            empty = QAction("(no devices found)", self.devices_menu)
            empty.setEnabled(False)
            self.devices_menu.addAction(empty)
            return
        for device in snap["nearby_devices"]:
            mac = device["mac"]
            mark = " ✓" if mac == connected_mac else ""
            action = QAction(f"{device['name']} [{mac[-8:]}]{mark}", self.devices_menu)
            action.triggered.connect(
                lambda _checked=False, m=mac: self._toggle_device(m)
            )
            self.devices_menu.addAction(action)

    def _toggle_device(self, mac: str) -> None:
        snap = self.state.snapshot()
        connected = snap["connected_device"]
        if connected and connected["mac"] == mac:
            self.ble.request_disconnect()
        else:
            self.ble.request_connect(mac)

    def tooltip(self) -> str:
        if self.state is None:
            return ""
        return f"Pendant: {self._summary(self.state.snapshot())}"
