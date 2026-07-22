"""KDE/Linux system tray for Chronicle capture and vault sync."""

import logging
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QSystemTrayIcon
from PySide6.QtCore import QUrl

from syncthing_manager import SyncthingManager
from vault_core import VaultSyncConfig, broker_pair, get_jwt_token, save_vault_dir

logger = logging.getLogger(__name__)
SCREENPIPE_DB = Path.home() / ".screenpipe/db.sqlite"
load_dotenv(Path(__file__).resolve().parent / ".env")


@dataclass
class SharedState:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    status: str = "idle"
    error: Optional[str] = None
    connected: bool = False
    completion: Optional[float] = None
    folder_error: Optional[str] = None
    folder_id: Optional[str] = None
    vault_dir: str = ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                key: value for key, value in vars(self).items() if key != "_lock"
            }

    def update(self, **values) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)


class VaultSyncManager:
    def __init__(self, state: SharedState) -> None:
        self.state = state
        self.config = VaultSyncConfig.from_env()
        self.syncthing = SyncthingManager()
        self.state.update(vault_dir=self.config.local_vault_dir)
        self._lock = threading.Lock()

    def pair_async(self) -> None:
        threading.Thread(target=self._pair, daemon=True).start()

    def _pair(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            cfg = self.config
            if not cfg.auth_username or not cfg.auth_password:
                self.state.update(status="error", error="set Chronicle login in .env")
                return
            self.state.update(status="starting", error=None)
            self.syncthing.start()
            self.state.update(status="pairing")
            token = get_jwt_token(cfg.auth_username, cfg.auth_password, cfg.backend_url)
            if not token:
                self.state.update(status="error", error="backend authentication failed")
                return
            info = broker_pair(
                cfg.backend_url, token, self.syncthing.device_id(), cfg.device_name
            )
            self.syncthing.ensure_server_device(
                info["server_device_id"],
                "Chronicle Server",
                [info["sync_address"]] if info.get("sync_address") else ["dynamic"],
            )
            self.syncthing.ensure_folder(
                info["folder_id"], cfg.local_vault_dir,
                info.get("folder_label", "Chronicle Vault"),
                info["server_device_id"], self.syncthing.device_id(),
            )
            self.state.update(status="syncing", folder_id=info["folder_id"], error=None)
        except (OSError, httpx.HTTPError, RuntimeError) as error:
            logger.exception("Vault pairing failed")
            self.state.update(status="error", error=str(error))
        finally:
            self._lock.release()

    def set_vault_dir(self, path: str) -> None:
        save_vault_dir(path)
        self.config.local_vault_dir = path
        self.state.update(vault_dir=path)
        self.pair_async()

    def refresh_status(self) -> None:
        if not self.syncthing.is_running():
            return
        snap = self.state.snapshot()
        status = (
            self.syncthing.folder_status(snap["folder_id"])
            if snap["folder_id"] else {}
        )
        self.state.update(
            connected=self.syncthing.connection_count() > 0,
            completion=status.get("completion"),
            folder_error=status.get("error"),
        )

    def shutdown(self) -> None:
        self.syncthing.stop()


def _unit_state(name: str) -> str:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", name], capture_output=True, text=True
    )
    return result.stdout.strip() or "unknown"


def _screenpipe_stats() -> str:
    if not SCREENPIPE_DB.exists():
        return "No local database"
    try:
        uri = f"file:{SCREENPIPE_DB.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
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
        size = sum(p.stat().st_size for p in SCREENPIPE_DB.parent.rglob("*") if p.is_file())
        return f"{frames:,} frames · {audio:,} audio chunks · {size / 1024**3:.1f} GiB"
    except (OSError, sqlite3.Error) as error:
        return f"Stats unavailable: {error}"


class ChronicleTray(QSystemTrayIcon):
    def __init__(self, state: SharedState, manager: VaultSyncManager) -> None:
        icon = QIcon.fromTheme("view-calendar-timeline", QIcon.fromTheme("folder-sync"))
        super().__init__(icon)
        self.state = state
        self.manager = manager
        menu = QMenu()
        self.capture_status = menu.addAction("ScreenPipe: checking…")
        self.collector_status = menu.addAction("Chronicle collector: checking…")
        self.stats = menu.addAction("Stats: checking…")
        for item in (self.capture_status, self.collector_status, self.stats):
            item.setEnabled(False)
        menu.addSeparator()
        self._service_actions(menu, "ScreenPipe", "screenpipe.service")
        self._service_actions(menu, "Collector", "chronicle-screenpipe.service")
        menu.addSeparator()
        self.sync_status = menu.addAction("Vault sync: starting…")
        self.sync_status.setEnabled(False)
        menu.addAction("Open vault in Obsidian", self.open_obsidian)
        menu.addAction("Choose vault folder…", self.choose_folder)
        menu.addAction("Sync now / re-pair", manager.pair_async)
        menu.addSeparator()
        menu.addAction(
            "Open Chronicle",
            lambda: QDesktopServices.openUrl(QUrl(manager.config.backend_url)),
        )
        menu.addAction("Quit tray", QApplication.quit)
        self.setContextMenu(menu)
        self.setToolTip("Chronicle")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.refresh()

    def _service_actions(self, menu: QMenu, label: str, unit: str) -> None:
        submenu = menu.addMenu(label)
        for title, verb in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
            action = QAction(title, submenu)
            action.triggered.connect(lambda _checked=False, v=verb, u=unit: self.service(v, u))
            submenu.addAction(action)

    def service(self, verb: str, unit: str) -> None:
        subprocess.run(["systemctl", "--user", verb, unit], check=False)
        QTimer.singleShot(500, self.refresh)

    def refresh(self) -> None:
        self.manager.refresh_status()
        capture = _unit_state("screenpipe.service")
        collector = _unit_state("chronicle-screenpipe.service")
        self.capture_status.setText(f"ScreenPipe: {capture}")
        self.collector_status.setText(f"Chronicle collector: {collector}")
        self.stats.setText(_screenpipe_stats())
        snap = self.state.snapshot()
        if snap["status"] == "error":
            sync = f"error — {snap['error']}"
        elif snap["completion"] is not None:
            sync = f"{snap['completion']:.0f}%"
        else:
            sync = snap["status"]
        self.sync_status.setText(f"Vault sync: {sync}")
        self.setToolTip(f"Chronicle\nScreenPipe: {capture}\nCollector: {collector}\nVault: {sync}")

    def open_obsidian(self) -> None:
        vault = self.state.snapshot()["vault_dir"]
        Path(vault).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl(f"obsidian://open?path={quote(vault)}"))

    def choose_folder(self) -> None:
        current = self.state.snapshot()["vault_dir"]
        chosen = QFileDialog.getExistingDirectory(None, "Choose Chronicle vault", current)
        if chosen:
            self.manager.set_vault_dir(chosen)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        raise SystemExit("No system tray is available in this desktop session")
    state = SharedState()
    manager = VaultSyncManager(state)
    manager.pair_async()
    tray = ChronicleTray(state, manager)
    tray.show()
    try:
        sys.exit(app.exec())
    finally:
        manager.shutdown()
