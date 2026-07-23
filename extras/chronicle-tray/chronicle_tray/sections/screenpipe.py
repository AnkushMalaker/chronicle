"""ScreenPipe section — local capture stats, service controls, and settings.

Shows frame/audio/storage counts from the local ScreenPipe database and
start/stop/restart controls for the capture engine and the Chronicle
collector. On Linux the section also edits capture settings in place: pause
timers for the engine, independent record/forward toggles per audio source,
and a screen-capture toggle (all systemd-unit / collector-config edits, so
they are Linux-only). On macOS ScreenPipe manages its own launchd service, so
only the stats and the collector control are shown.
"""

import json
import logging
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

from chronicle_tray.paths import add_repo_root
from chronicle_tray.screenpipe_settings import (
    _audio_sources,
    _capture_settings,
    _forward_audio_setting,
    _save_capture_settings,
    _save_forward_audio_setting,
    _updated_audio_modes,
)
from chronicle_tray.sections import Section

logger = logging.getLogger(__name__)
SCREENPIPE_DB = Path.home() / ".screenpipe/db.sqlite"
IS_LINUX = sys.platform.startswith("linux")


def _unit_state(name: str) -> str:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", name], capture_output=True, text=True
    )
    return result.stdout.strip() or "unknown"


def _stats() -> str:
    if not SCREENPIPE_DB.exists():
        return "No local database"
    try:
        uri = f"file:{SCREENPIPE_DB.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            frames = (
                db.execute("SELECT count(*) FROM frames").fetchone()[0]
                if "frames" in tables
                else 0
            )
            audio = (
                db.execute("SELECT count(*) FROM audio_chunks").fetchone()[0]
                if "audio_chunks" in tables
                else 0
            )
        size = sum(
            p.stat().st_size for p in SCREENPIPE_DB.parent.rglob("*") if p.is_file()
        )
        return f"{frames:,} frames · {audio:,} audio chunks · {size / 1024**3:.1f} GiB"
    except (OSError, sqlite3.Error) as error:
        return f"Stats unavailable: {error}"


class ScreenPipeSection(Section):
    title = "ScreenPipe"

    def __init__(self) -> None:
        self.engine_status = None
        self.collector_status = None
        self.stats_item = None
        self.pause_menu = None
        self.pause_timer = None
        self.unit_actions: dict[str, dict[str, QAction]] = {}
        self.audio_actions: dict[str, dict[str, QAction]] = {}
        self.screen_capture = None

    def available(self) -> tuple[bool, str]:
        if shutil.which("screenpipe") or SCREENPIPE_DB.exists():
            return True, ""
        return (
            False,
            "ScreenPipe: not installed — curl -fsSL get.screenpi.pe/cli | sh",
        )

    def build(self, menu: QMenu) -> None:
        if IS_LINUX:
            self.engine_status = menu.addAction("ScreenPipe: checking…")
            self.engine_status.setEnabled(False)
        self.collector_status = menu.addAction("Chronicle collector: checking…")
        self.collector_status.setEnabled(False)
        self.stats_item = menu.addAction("Stats: checking…")
        self.stats_item.setEnabled(False)
        if IS_LINUX:
            engine = self._unit_actions(menu, "ScreenPipe", "screenpipe.service")
            self._pause_actions(engine)
            self._unit_actions(menu, "Collector", "chronicle-screenpipe.service")
            self._settings_actions(menu)
        else:
            self._collector_actions_macos(menu)

    def _unit_actions(self, menu: QMenu, label: str, unit: str) -> QMenu:
        submenu = menu.addMenu(label)
        actions = {}
        for title, verb in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
            action = QAction(title, submenu)
            action.triggered.connect(
                lambda _checked=False, v=verb, u=unit: self._unit(v, u)
            )
            submenu.addAction(action)
            actions[verb] = action
        self.unit_actions[unit] = actions
        return submenu

    def _pause_actions(self, submenu: QMenu) -> None:
        self.pause_timer = QTimer()
        self.pause_timer.setSingleShot(True)
        self.pause_timer.timeout.connect(
            lambda: self._unit("start", "screenpipe.service")
        )
        self.pause_menu = submenu.addMenu("Pause for")
        for title, minutes in (
            ("5 minutes", 5),
            ("15 minutes", 15),
            ("30 minutes", 30),
            ("1 hour", 60),
            ("2 hours", 120),
            ("8 hours", 480),
        ):
            self.pause_menu.addAction(
                title, lambda _checked=False, m=minutes: self._pause_capture(m)
            )

    def _settings_actions(self, menu: QMenu) -> None:
        settings_menu = menu.addMenu("Settings")
        audio_menu = settings_menu.addMenu("Audio")
        for source, title in (("system", "System audio"), ("mic", "Microphone")):
            source_menu = audio_menu.addMenu(title)
            record = source_menu.addAction("Record locally")
            record.setCheckable(True)
            record.triggered.connect(
                lambda checked=False, selected=source: self._save_audio_source(
                    selected, "record", checked
                )
            )
            forward = source_menu.addAction("Send to Chronicle")
            forward.setCheckable(True)
            forward.triggered.connect(
                lambda checked=False, selected=source: self._save_audio_source(
                    selected, "forward", checked
                )
            )
            self.audio_actions[source] = {"record": record, "forward": forward}
        self.screen_capture = settings_menu.addAction("Screen capture")
        self.screen_capture.setCheckable(True)
        self.screen_capture.triggered.connect(self._save_screen_capture)

    def _unit(self, verb: str, unit: str) -> None:
        if (
            self.pause_timer is not None
            and unit == "screenpipe.service"
            and verb in {"start", "restart"}
        ):
            self.pause_timer.stop()
        subprocess.run(["systemctl", "--user", verb, unit], check=False)
        QTimer.singleShot(500, self.refresh)

    def _pause_capture(self, minutes: int) -> None:
        self._unit("stop", "screenpipe.service")
        self.pause_timer.start(minutes * 60 * 1000)

    def _save_screen_capture(self) -> None:
        try:
            was_active = _unit_state("screenpipe.service") == "active"
            _save_capture_settings(
                _capture_settings()[0], self.screen_capture.isChecked()
            )
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            if was_active:
                self._unit("restart", "screenpipe.service")
        except (OSError, StopIteration, ValueError, subprocess.CalledProcessError):
            logger.exception("Could not save ScreenPipe settings")
            self._refresh_capture_settings()

    def _save_audio_source(self, source: str, setting: str, enabled: bool) -> None:
        try:
            was_capture_active = _unit_state("screenpipe.service") == "active"
            was_collector_active = (
                _unit_state("chronicle-screenpipe.service") == "active"
            )
            capture_mode, screen_enabled = _capture_settings()
            forwarding_mode = _forward_audio_setting()
            capture_mode, forwarding_mode = _updated_audio_modes(
                capture_mode, forwarding_mode, source, setting, enabled
            )
            _save_capture_settings(capture_mode, screen_enabled)
            _save_forward_audio_setting(forwarding_mode)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            if was_capture_active:
                self._unit("restart", "screenpipe.service")
            if was_collector_active:
                self._unit("restart", "chronicle-screenpipe.service")
        except (
            OSError,
            StopIteration,
            ValueError,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
        ):
            logger.exception("Could not save audio settings")
            self._refresh_capture_settings()

    def _refresh_capture_settings(self) -> None:
        if not self.audio_actions:
            return
        try:
            audio_mode, screen_enabled = _capture_settings()
            captured = _audio_sources(audio_mode)
            forwarded = _audio_sources(_forward_audio_setting(), forwarding=True)
            for source, actions in self.audio_actions.items():
                actions["record"].setChecked(source in captured)
                actions["forward"].setChecked(source in forwarded)
                actions["record"].setEnabled(True)
                actions["forward"].setEnabled(True)
            self.screen_capture.setChecked(screen_enabled)
            self.screen_capture.setEnabled(True)
        except (OSError, KeyError, StopIteration, ValueError, json.JSONDecodeError):
            logger.exception("Could not read audio settings")
            for actions in self.audio_actions.values():
                actions["record"].setEnabled(False)
                actions["forward"].setEnabled(False)
            self.screen_capture.setEnabled(False)

    def _collector_actions_macos(self, menu: QMenu) -> None:
        submenu = menu.addMenu("Collector")
        for title, verb in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
            action = QAction(title, submenu)
            action.triggered.connect(
                lambda _checked=False, v=verb: self._collector_macos(v)
            )
            submenu.addAction(action)

    def _collector_macos(self, verb: str) -> None:
        add_repo_root()
        import clients

        clients.component_action("screenpipe-collector", verb)
        QTimer.singleShot(500, self.refresh)

    def _collector_state(self) -> str:
        if IS_LINUX:
            return _unit_state("chronicle-screenpipe.service")
        add_repo_root()
        import clients

        status = clients.component_status("screenpipe-collector")
        if not status["installed"]:
            return "not installed"
        return "active" if status["active"] else "inactive"

    def refresh(self) -> None:
        if self.engine_status is not None:
            engine = _unit_state("screenpipe.service")
            self.engine_status.setText(f"ScreenPipe: {engine}")
            if self.pause_menu is not None:
                self.pause_menu.setEnabled(engine == "active")
        collector = self._collector_state()
        self.collector_status.setText(f"Chronicle collector: {collector}")
        for unit, actions in self.unit_actions.items():
            active = _unit_state(unit) == "active"
            actions["start"].setEnabled(not active)
            actions["stop"].setEnabled(active)
        self.stats_item.setText(_stats())
        self._refresh_capture_settings()

    def tooltip(self) -> str:
        return f"Collector: {self._collector_state()}"

    def shutdown(self) -> None:
        if self.pause_timer is not None:
            self.pause_timer.stop()
