"""ScreenPipe section — local capture stats, service controls, and settings.

Shows frame/audio/storage counts from the local ScreenPipe database, plus
start/stop/restart controls for the recorder and the Chronicle collector, pause
timers, master audio/video switches, and a per-source record/forward dialog.

Both services are client components (clients.py), which installs them as systemd
user units on Linux and launchd agents on macOS, so this menu is identical on
both. Capture settings edit the recorder's argv in its component spec rather
than a unit file, for the same reason.
"""

import json
import logging
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

from chronicle_tray import recorder_update
from chronicle_tray.capture_settings_dialog import CaptureSettingsDialog
from chronicle_tray.paths import add_repo_root
from chronicle_tray.screenpipe_settings import (
    _audio_modes,
    _audio_sources,
    _capture_settings,
    _forward_audio_setting,
    _save_capture_settings,
    _save_forward_audio_setting,
    _toggled_audio_modes,
)
from chronicle_tray.sections import Section
from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

logger = logging.getLogger(__name__)
SCREENPIPE_DB = Path.home() / ".screenpipe/db.sqlite"
# Recorder and collector are both client components (clients.py), so the same
# controls work over systemd units and launchd agents alike.
RECORDER = "screenpipe"
COLLECTOR = "screenpipe-collector"


def _clients():
    add_repo_root()
    import clients

    return clients


def _component_state(name: str) -> str:
    status = _clients().component_status(name)
    if not status["installed"]:
        return "not installed"
    return "active" if status["active"] else "inactive"


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
        self.audio_capture = None
        self.video_capture = None
        self.settings_action = None
        # What to restore when the master audio switch is turned back on.
        self.audio_restore = ("both", "both")
        self.update_action = None
        self.revert_action = None
        # Written by the update worker thread, rendered by refresh().
        self._update_lock = threading.Lock()
        self._update_state = {"busy": False, "message": ""}

    def available(self) -> tuple[bool, str]:
        if shutil.which("screenpipe") or SCREENPIPE_DB.exists():
            return True, ""
        return (
            False,
            "ScreenPipe: not installed — curl -fsSL get.screenpi.pe/cli | sh",
        )

    def build(self, menu: QMenu) -> None:
        self.engine_status = menu.addAction("ScreenPipe: checking…")
        self.engine_status.setEnabled(False)
        self.collector_status = menu.addAction("Chronicle collector: checking…")
        self.collector_status.setEnabled(False)
        self.stats_item = menu.addAction("Stats: checking…")
        self.stats_item.setEnabled(False)
        engine = self._unit_actions(menu, "ScreenPipe", RECORDER)
        self._pause_actions(engine)
        self._unit_actions(menu, "Collector", COLLECTOR)
        self._capture_actions(menu)

    def _unit_actions(self, menu: QMenu, label: str, component: str) -> QMenu:
        submenu = menu.addMenu(label)
        actions = {}
        for title, verb in (
            ("Start", "start"),
            ("Stop", "stop"),
            ("Restart", "restart"),
        ):
            action = QAction(title, submenu)
            action.triggered.connect(
                lambda _checked=False, v=verb, c=component: self._unit(v, c)
            )
            submenu.addAction(action)
            actions[verb] = action
        self.unit_actions[component] = actions
        return submenu

    def _pause_actions(self, submenu: QMenu) -> None:
        self.pause_timer = QTimer()
        self.pause_timer.setSingleShot(True)
        self.pause_timer.timeout.connect(lambda: self._unit("start", RECORDER))
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

    def _capture_actions(self, menu: QMenu) -> None:
        """Master on/off switches in the menu; the detail lives in the dialog."""
        capture_menu = menu.addMenu("Capture")
        self.audio_capture = capture_menu.addAction("Audio capture")
        self.audio_capture.setCheckable(True)
        self.audio_capture.triggered.connect(self._toggle_audio_capture)
        self.video_capture = capture_menu.addAction("Video capture")
        self.video_capture.setCheckable(True)
        self.video_capture.triggered.connect(self._toggle_video_capture)
        self.settings_action = menu.addAction(
            "Capture settings…", self._open_capture_settings
        )
        self.update_action = menu.addAction("Update recorder…", self._update_recorder)
        self.revert_action = menu.addAction(
            "Revert recorder update", self._revert_recorder
        )

    def _unit(self, verb: str, component: str) -> None:
        if (
            self.pause_timer is not None
            and component == RECORDER
            and verb in {"start", "restart"}
        ):
            self.pause_timer.stop()
        _clients().component_action(component, verb)
        QTimer.singleShot(500, self.refresh)

    def _pause_capture(self, minutes: int) -> None:
        self._unit("stop", RECORDER)
        self.pause_timer.start(minutes * 60 * 1000)

    def _edit_capture_settings(self, plan) -> None:
        """Read the live settings, run ``plan`` over them, persist the result.

        ``plan(capture_mode, forwarding_mode, screen_enabled)`` returns the same
        triple, or None to leave everything alone (a cancelled dialog). Reading
        and writing share one guard so a half-applied edit can't leave the two
        config files disagreeing; the services are restarted once, at the end,
        and only if they were running.
        """
        try:
            capture_mode, screen_enabled = _capture_settings()
            forwarding_mode = _forward_audio_setting()
            planned = plan(capture_mode, forwarding_mode, screen_enabled)
            if planned is None:
                return
            capture_mode, forwarding_mode, screen_enabled = planned
            was_capture_active = _component_state(RECORDER) == "active"
            was_collector_active = _component_state(COLLECTOR) == "active"
            # Rewrites the spec and regenerates the unit/plist.
            _save_capture_settings(capture_mode, screen_enabled)
            _save_forward_audio_setting(forwarding_mode)
            if was_capture_active:
                self._unit("restart", RECORDER)
            if was_collector_active:
                self._unit("restart", COLLECTOR)
        except (
            OSError,
            StopIteration,
            ValueError,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
        ):
            logger.exception("Could not save capture settings")
        finally:
            self._refresh_capture_settings()

    def _toggle_audio_capture(self, enabled: bool) -> None:
        def plan(capture_mode, forwarding_mode, screen_enabled):
            if not enabled:
                # Remember the per-source choices so switching back on restores
                # them rather than turning every source on.
                self.audio_restore = (capture_mode, forwarding_mode)
            capture_mode, forwarding_mode = _toggled_audio_modes(
                capture_mode, forwarding_mode, enabled, self.audio_restore
            )
            return capture_mode, forwarding_mode, screen_enabled

        self._edit_capture_settings(plan)

    def _toggle_video_capture(self, enabled: bool) -> None:
        self._edit_capture_settings(
            lambda capture_mode, forwarding_mode, _screen: (
                capture_mode,
                forwarding_mode,
                enabled,
            )
        )

    def _open_capture_settings(self) -> None:
        def plan(capture_mode, forwarding_mode, screen_enabled):
            dialog = CaptureSettingsDialog(
                _audio_sources(capture_mode),
                _audio_sources(forwarding_mode, forwarding=True),
                screen_enabled,
            )
            if not dialog.exec():
                return None
            captured, forwarded, screen_enabled = dialog.settings()
            capture_mode, forwarding_mode = _audio_modes(captured, forwarded)
            return capture_mode, forwarding_mode, screen_enabled

        self._edit_capture_settings(plan)

    def _refresh_capture_settings(self) -> None:
        if self.audio_capture is None:
            return
        try:
            audio_mode, screen_enabled = _capture_settings()
            _forward_audio_setting()  # unreadable ⇒ nothing here can be edited
            self.audio_capture.setChecked(audio_mode != "off")
            self.video_capture.setChecked(screen_enabled)
            for action in (self.audio_capture, self.video_capture):
                action.setEnabled(True)
            self.settings_action.setEnabled(True)
        except (OSError, KeyError, StopIteration, ValueError, json.JSONDecodeError):
            logger.exception("Could not read capture settings")
            for action in (self.audio_capture, self.video_capture):
                action.setEnabled(False)
            self.settings_action.setEnabled(False)

    def _update_recorder(self) -> None:
        self._run_update_step(self._update_worker)

    def _revert_recorder(self) -> None:
        self._run_update_step(self._revert_worker)

    def _run_update_step(self, worker) -> None:
        """Start ``worker`` on a daemon thread; refresh() renders its state.

        The download is tens of MB, so it cannot run on the menu action
        directly. One step at a time — a second click while busy is ignored
        rather than queued.
        """
        with self._update_lock:
            if self._update_state["busy"]:
                return
        self._set_update_state(True, "working…")
        threading.Thread(target=worker, daemon=True).start()
        self.refresh()

    def _set_update_state(self, busy: bool, message: str) -> None:
        with self._update_lock:
            self._update_state = {"busy": busy, "message": message}

    def _update_worker(self) -> None:
        try:
            _current, latest, available = recorder_update.check()
            if not available:
                self._set_update_state(False, f"up to date ({latest['describe']})")
                return
            self._set_update_state(True, "downloading…")
            recorder_update.install(latest)
            self._set_update_state(False, f"updated to {latest['describe']}")
        except Exception as error:  # rendered in the menu, never raised into Qt
            logger.exception("recorder update failed")
            self._set_update_state(False, f"update failed: {error}")

    def _revert_worker(self) -> None:
        try:
            manifest = recorder_update.revert()
            name = manifest["describe"] if manifest else "previous build"
            self._set_update_state(False, f"reverted to {name}")
        except Exception as error:
            logger.exception("recorder revert failed")
            self._set_update_state(False, f"revert failed: {error}")

    def _refresh_update_actions(self) -> None:
        if self.update_action is None:
            return
        with self._update_lock:
            busy = self._update_state["busy"]
            message = self._update_state["message"]
        label = "Update recorder…"
        if message:
            label = f"Update recorder… ({message})"
        self.update_action.setText(label)
        self.update_action.setEnabled(not busy)
        self.revert_action.setEnabled(not busy and recorder_update.can_revert())

    def _collector_state(self) -> str:
        return _component_state(COLLECTOR)

    def refresh(self) -> None:
        if self.engine_status is not None:
            engine = _component_state(RECORDER)
            self.engine_status.setText(f"ScreenPipe: {engine}")
            if self.pause_menu is not None:
                self.pause_menu.setEnabled(engine == "active")
        collector = self._collector_state()
        self.collector_status.setText(f"Chronicle collector: {collector}")
        for component, actions in self.unit_actions.items():
            active = _component_state(component) == "active"
            actions["start"].setEnabled(not active)
            actions["stop"].setEnabled(active)
        self.stats_item.setText(_stats())
        self._refresh_capture_settings()
        self._refresh_update_actions()

    def tooltip(self) -> str:
        return f"Collector: {self._collector_state()}"

    def shutdown(self) -> None:
        if self.pause_timer is not None:
            self.pause_timer.stop()
