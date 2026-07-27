"""Vault sync section — Obsidian vault ↔ Chronicle server via private Syncthing.

Reuses the vault-sync project's core (vault_core + syncthing_manager) in place;
this module is only the Qt menu glue. Client configuration comes from the
repository-root .env shared by Chronicle's native client components.
"""

import logging
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from chronicle_tray.paths import REPO_ROOT, VAULT_SYNC_DIR, add_vault_path
from chronicle_tray.sections import Section
from dotenv import load_dotenv
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMenu

logger = logging.getLogger(__name__)


def _load_vault_environment() -> None:
    """Load the canonical client configuration before constructing the manager."""
    load_dotenv(REPO_ROOT / ".env")


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
            return {key: value for key, value in vars(self).items() if key != "_lock"}

    def update(self, **values) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)


class VaultSyncManager:
    def __init__(self, state: SharedState) -> None:
        from syncthing_manager import SyncthingManager
        from vault_core import VaultSyncConfig

        self.state = state
        self.config = VaultSyncConfig.from_env()
        self.syncthing = SyncthingManager()
        self.state.update(vault_dir=self.config.local_vault_dir)
        self._lock = threading.Lock()

    def pair_async(self) -> None:
        threading.Thread(target=self._pair, daemon=True).start()

    def _pair(self) -> None:
        from vault_core import broker_pair

        if not self._lock.acquire(blocking=False):
            return
        try:
            cfg = self.config
            if not cfg.api_key:
                self.state.update(
                    status="error",
                    error="set CHRONICLE_API_KEY in the repository-root .env",
                )
                return
            self.state.update(status="starting", error=None)
            self.syncthing.start()
            self.state.update(status="pairing")
            info = broker_pair(
                cfg.backend_url,
                cfg.api_key,
                self.syncthing.device_id(),
                cfg.device_name,
            )
            self.syncthing.ensure_server_device(
                info["server_device_id"],
                "Chronicle Server",
                [info["sync_address"]] if info.get("sync_address") else ["dynamic"],
            )
            self.syncthing.ensure_folder(
                info["folder_id"],
                cfg.local_vault_dir,
                info.get("folder_label", "Chronicle Vault"),
                info["server_device_id"],
                self.syncthing.device_id(),
            )
            self.state.update(status="syncing", folder_id=info["folder_id"], error=None)
        except (OSError, httpx.HTTPError, RuntimeError) as error:
            logger.exception("Vault pairing failed")
            self.state.update(status="error", error=str(error))
        finally:
            self._lock.release()

    def set_vault_dir(self, path: str) -> None:
        from vault_core import save_vault_dir

        save_vault_dir(path)
        self.config.local_vault_dir = path
        self.state.update(vault_dir=path)
        self.pair_async()

    def refresh_status(self) -> None:
        if not self.syncthing.is_running():
            return
        snap = self.state.snapshot()
        status = (
            self.syncthing.folder_status(snap["folder_id"]) if snap["folder_id"] else {}
        )
        self.state.update(
            connected=self.syncthing.connection_count() > 0,
            completion=status.get("completion"),
            folder_error=status.get("error"),
        )

    def shutdown(self) -> None:
        self.syncthing.stop()


class VaultSection(Section):
    title = "Vault Sync"

    def __init__(self) -> None:
        self.state = SharedState()
        self.manager: Optional[VaultSyncManager] = None
        self.status_item = None

    def available(self) -> tuple[bool, str]:
        if not VAULT_SYNC_DIR.exists():
            return False, "Vault sync: extras/vault-sync missing from checkout"
        if shutil.which("syncthing") is None:
            hint = (
                "brew install syncthing"
                if sys.platform == "darwin"
                else "install syncthing via your package manager"
            )
            return False, f"Vault sync: syncthing not found — {hint}"
        return True, ""

    def build(self, menu: QMenu) -> None:
        add_vault_path()
        _load_vault_environment()
        self.manager = VaultSyncManager(self.state)
        self.manager.pair_async()

        self.status_item = menu.addAction("Vault sync: starting…")
        self.status_item.setEnabled(False)
        menu.addAction("Open vault in Obsidian", self._open_obsidian)
        menu.addAction("Choose vault folder…", self._choose_folder)
        menu.addAction("Sync now / re-pair", self.manager.pair_async)
        menu.addAction("Open Syncthing UI", self._open_syncthing)

    def refresh(self) -> None:
        if self.manager is None:
            return
        self.manager.refresh_status()
        self.status_item.setText(f"Vault sync: {self._summary()}")

    def _summary(self) -> str:
        snap = self.state.snapshot()
        if snap["status"] == "error":
            return f"error — {snap['error']}"
        if snap["folder_error"]:
            error = snap["folder_error"]
            return f"folder error — {error[:80]}"
        if snap["completion"] is not None:
            if snap["completion"] >= 99.9 and snap["connected"]:
                return "in sync"
            return f"{snap['completion']:.0f}%"
        return snap["status"]

    def tooltip(self) -> str:
        return f"Vault: {self._summary()}"

    def backend_url(self) -> str:
        return self.manager.config.backend_url if self.manager else ""

    def _open_obsidian(self) -> None:
        vault = self.state.snapshot()["vault_dir"]
        Path(vault).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl(f"obsidian://open?path={quote(vault)}"))

    def _choose_folder(self) -> None:
        current = self.state.snapshot()["vault_dir"]
        chosen = QFileDialog.getExistingDirectory(
            None, "Choose Chronicle vault", current
        )
        if chosen and self.manager:
            self.manager.set_vault_dir(chosen)

    def _open_syncthing(self) -> None:
        if self.manager:
            QDesktopServices.openUrl(QUrl(self.manager.syncthing.base_url))

    def shutdown(self) -> None:
        if self.manager:
            self.manager.shutdown()
