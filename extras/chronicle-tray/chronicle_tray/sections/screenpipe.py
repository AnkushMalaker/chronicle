"""ScreenPipe section — local capture stats + service controls.

Shows frame/audio/storage counts from the local ScreenPipe database and
start/stop/restart controls for the capture engine and the Chronicle
collector. Unit control is Linux/systemd; on macOS ScreenPipe manages its own
launchd service, so only the stats and the collector control are shown.
"""

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

from chronicle_tray.paths import add_repo_root
from chronicle_tray.sections import Section

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
            self._unit_actions(menu, "ScreenPipe", "screenpipe.service")
            self._unit_actions(menu, "Collector", "chronicle-screenpipe.service")
        else:
            self._collector_actions_macos(menu)

    def _unit_actions(self, menu: QMenu, label: str, unit: str) -> None:
        submenu = menu.addMenu(label)
        for title, verb in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
            action = QAction(title, submenu)
            action.triggered.connect(
                lambda _checked=False, v=verb, u=unit: self._unit(v, u)
            )
            submenu.addAction(action)

    def _unit(self, verb: str, unit: str) -> None:
        subprocess.run(["systemctl", "--user", verb, unit], check=False)
        QTimer.singleShot(500, self.refresh)

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
            self.engine_status.setText(f"ScreenPipe: {_unit_state('screenpipe.service')}")
        self.collector_status.setText(f"Chronicle collector: {self._collector_state()}")
        self.stats_item.setText(_stats())

    def tooltip(self) -> str:
        return f"Collector: {self._collector_state()}"
