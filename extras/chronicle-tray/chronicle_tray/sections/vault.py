"""Vault sync section — Obsidian vault ↔ Chronicle server via private Syncthing.

Wraps the chronicle-vault-sync package; this module is only the Qt menu glue. Client configuration comes from the
repository-root .env shared by Chronicle's native client components.
"""

import importlib.util
import logging
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from chronicle_client import load_client_env
from chronicle_tray.macos import activate_for_dialog
from chronicle_tray.obsidian import register_obsidian_vault
from chronicle_tray.sections import Section
from chronicle_vault_sync import (
    SyncthingManager,
    VaultSyncConfig,
    broker_folders,
    broker_pair,
    broker_space_action,
    persisted_vault_dir,
    save_vault_dir,
)
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMenu

logger = logging.getLogger(__name__)


def _load_vault_environment() -> None:
    """Load the canonical client configuration before constructing the manager."""
    load_client_env()


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
    folders: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        with self._lock:
            return {key: value for key, value in vars(self).items() if key != "_lock"}

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
        self._inventory_lock = threading.Lock()

    def pair_async(self, memory_space_id: Optional[str] = None) -> None:
        threading.Thread(
            target=self._pair, args=(memory_space_id,), daemon=True
        ).start()

    def _folder_inventory(self) -> list[dict]:
        return broker_folders(self.config.backend_url, self.config.api_key)

    def _local_dir(self, folder: dict) -> str:
        space_id = folder.get("memory_space_id")
        if not space_id:
            return self.config.local_vault_dir
        persisted = persisted_vault_dir(space_id)
        if persisted:
            return persisted
        name = re.sub(r"[^a-zA-Z0-9._-]+", "-", folder.get("name") or "Space")
        return str(
            Path(self.config.local_vault_dir).parent
            / "Chronicle Spaces"
            / f"{name}-{space_id[:8]}"
        )

    def _pair(self, memory_space_id: Optional[str] = None) -> None:
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
            inventory = self._folder_inventory()
            target = next(
                (
                    item
                    for item in inventory
                    if item.get("memory_space_id") == memory_space_id
                ),
                None,
            )
            if target is None:
                raise RuntimeError("Vault folder is no longer available")
            local_dir = self._local_dir(target)
            info = broker_pair(
                cfg.backend_url,
                cfg.api_key,
                self.syncthing.device_id(),
                cfg.device_name,
                memory_space_id,
            )
            self.syncthing.ensure_server_device(
                info["server_device_id"],
                "Chronicle Server",
                [info["sync_address"]] if info.get("sync_address") else ["dynamic"],
            )
            self.syncthing.ensure_folder(
                info["folder_id"],
                local_dir,
                info.get("folder_label", "Chronicle Vault"),
                info["server_device_id"],
                self.syncthing.device_id(),
            )
            sync_health = None
            if memory_space_id is not None:
                try:
                    sync_health = broker_space_action(
                        cfg.backend_url,
                        cfg.api_key,
                        memory_space_id,
                        "rescan",
                    )
                except (OSError, httpx.HTTPError, RuntimeError):
                    # Local pairing succeeded. Keep the folder retryable and let
                    # the next refresh/manual rescan surface a server-side issue.
                    logger.warning(
                        "Paired memory-space vault but could not acknowledge server rescan",
                        exc_info=True,
                    )
            key = memory_space_id or "main"
            folders = dict(self.state.snapshot().get("folders") or {})
            folders[key] = {
                **target,
                **info,
                **(
                    {
                        "sync_state": (
                            "healthy" if sync_health.get("healthy") else "error"
                        ),
                        "sync_error": sync_health.get("error"),
                    }
                    if sync_health is not None
                    else {}
                ),
                "local_dir": local_dir,
                "paired": True,
                "completion": None,
                "folder_error": None,
            }
            self.state.update(
                status="syncing",
                folder_id=(
                    info["folder_id"]
                    if memory_space_id is None
                    else self.state.folder_id
                ),
                error=None,
                folders=folders,
            )
        except (OSError, httpx.HTTPError, RuntimeError) as error:
            logger.exception("Vault pairing failed")
            self.state.update(status="error", error=str(error))
        finally:
            self._lock.release()

    def set_vault_dir(self, path: str, memory_space_id: Optional[str] = None) -> None:
        save_vault_dir(path, memory_space_id)
        if memory_space_id is None:
            self.config.local_vault_dir = path
            self.state.update(vault_dir=path)
        self.pair_async(memory_space_id)

    def load_folders_async(self) -> None:
        threading.Thread(target=self._load_folders, daemon=True).start()

    def _load_folders(self) -> None:
        if not self._inventory_lock.acquire(blocking=False):
            return
        reconcile_space_ids: list[str] = []
        try:
            inventory = self._folder_inventory()
            existing = self.state.snapshot().get("folders") or {}
            folders = {}
            for item in inventory:
                key = item.get("memory_space_id") or "main"
                folder = {
                    **item,
                    **existing.get(key, {}),
                    "local_dir": existing.get(key, {}).get("local_dir")
                    or self._local_dir(item),
                }
                folders[key] = folder
                if (
                    item.get("memory_space_id")
                    and item.get("state") == "active"
                    and not folder.get("paired")
                ):
                    reconcile_space_ids.append(item["memory_space_id"])
            self.state.update(folders=folders)
        except (OSError, httpx.HTTPError, RuntimeError) as error:
            self.state.update(error=str(error))
        finally:
            self._inventory_lock.release()
        # The server inventory is the device's authorization boundary. Reconcile
        # every newly discovered active space after releasing the inventory lock;
        # pairing is idempotent, and a busy pair lock will be retried on refresh.
        for space_id in reconcile_space_ids:
            self._pair(space_id)

    def space_action_async(self, memory_space_id: str, action: str) -> None:
        threading.Thread(
            target=self._space_action,
            args=(memory_space_id, action),
            daemon=True,
        ).start()

    def _space_action(self, memory_space_id: str, action: str) -> None:
        try:
            broker_space_action(
                self.config.backend_url,
                self.config.api_key,
                memory_space_id,
                action,
            )
            self._load_folders()
            if action in {"resume", "reopen"}:
                self._pair(memory_space_id)
        except (OSError, httpx.HTTPError, RuntimeError, ValueError) as error:
            logger.exception("Memory-space vault action failed")
            self.state.update(status="error", error=str(error))

    def refresh_status(self) -> None:
        if not self.syncthing.is_running():
            return
        snap = self.state.snapshot()
        folders = dict(snap.get("folders") or {})
        for key, folder in folders.items():
            folder_id = folder.get("folder_id")
            if not folder_id or not folder.get("paired"):
                continue
            status = self.syncthing.folder_status(folder_id)
            folder["completion"] = status.get("completion")
            folder["folder_error"] = status.get("error")
            if folder.get("sync_state") == "frozen":
                try:
                    self.syncthing.set_folder_paused(folder_id, True)
                except httpx.HTTPError as error:
                    folder["folder_error"] = str(error)
        main_status = folders.get("main", {})
        self.state.update(
            connected=self.syncthing.connection_count() > 0,
            completion=main_status.get("completion"),
            folder_error=main_status.get("folder_error"),
            folders=folders,
        )

    def shutdown(self) -> None:
        self.syncthing.stop()


class VaultSection(Section):
    title = "Vault Sync"

    def __init__(self) -> None:
        self.state = SharedState()
        self.manager: Optional[VaultSyncManager] = None
        self.status_item = None
        self.spaces_menu: Optional[QMenu] = None
        self._spaces_signature: tuple = ()

    def available(self) -> tuple[bool, str]:
        if importlib.util.find_spec("chronicle_vault_sync") is None:
            return False, "Vault sync: chronicle-vault-sync is not installed"
        if shutil.which("syncthing") is None:
            hint = (
                "brew install syncthing"
                if sys.platform == "darwin"
                else "install syncthing via your package manager"
            )
            return False, f"Vault sync: syncthing not found — {hint}"
        return True, ""

    def build(self, menu: QMenu) -> None:
        _load_vault_environment()
        self.manager = VaultSyncManager(self.state)
        self.manager.pair_async()
        self.manager.load_folders_async()

        self.status_item = menu.addAction("Vault sync: starting…")
        self.status_item.setEnabled(False)
        menu.addAction("Open vault in Obsidian", self._open_obsidian)
        menu.addAction("Choose vault folder…", self._choose_folder)
        menu.addAction("Sync now / re-pair", self.manager.pair_async)
        self.spaces_menu = menu.addMenu("Memory spaces")
        self.spaces_menu.addAction("Loading…").setEnabled(False)
        menu.addAction("Open Syncthing UI", self._open_syncthing)

    def refresh(self) -> None:
        if self.manager is None:
            return
        self.manager.refresh_status()
        self.manager.load_folders_async()
        self.status_item.setText(f"Vault sync: {self._summary()}")
        self._refresh_spaces_menu()

    def _refresh_spaces_menu(self) -> None:
        if self.spaces_menu is None or self.manager is None:
            return
        folders = self.state.snapshot().get("folders") or {}
        spaces = [item for key, item in folders.items() if key != "main"]
        signature = tuple(
            sorted(
                (
                    item.get("memory_space_id"),
                    item.get("state"),
                    item.get("sync_state"),
                    item.get("paired"),
                    item.get("completion"),
                    item.get("folder_error"),
                )
                for item in spaces
            )
        )
        if signature == self._spaces_signature:
            return
        self._spaces_signature = signature
        self.spaces_menu.clear()
        if not spaces:
            self.spaces_menu.addAction("No memory spaces").setEnabled(False)
            return
        for folder in sorted(spaces, key=lambda item: item.get("name", "").casefold()):
            space_menu = self.spaces_menu.addMenu(folder.get("name") or "Memory space")
            state = folder.get("state", "active")
            sync_state = folder.get("sync_state", "unpaired")
            completion = folder.get("completion")
            label = sync_state
            if completion is not None and sync_state != "frozen":
                label = f"{completion:.0f}% synced"
            status = space_menu.addAction(f"{state} · {label}")
            status.setEnabled(False)
            space_id = folder["memory_space_id"]
            space_menu.addAction(
                "Open in Obsidian",
                lambda checked=False, sid=space_id: self._open_space(sid),
            )
            if state == "active":
                space_menu.addAction(
                    "Pair / sync now",
                    lambda checked=False, sid=space_id: self.manager.pair_async(sid),
                )
                space_menu.addAction(
                    "Choose local folder…",
                    lambda checked=False, sid=space_id: self._choose_space_folder(sid),
                )
                if sync_state != "unpaired":
                    lifecycle_action = "resume" if sync_state == "frozen" else "freeze"
                    lifecycle_label = (
                        "Resume sync" if sync_state == "frozen" else "Freeze sync"
                    )
                    space_menu.addAction(
                        lifecycle_label,
                        lambda checked=False, sid=space_id, action=lifecycle_action: self.manager.space_action_async(
                            sid, action
                        ),
                    )
            elif state == "archived":
                space_menu.addAction(
                    "Reopen editing cycle",
                    lambda checked=False, sid=space_id: self.manager.space_action_async(
                        sid, "reopen"
                    ),
                )

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

    def _open_space(self, memory_space_id: str) -> None:
        folders = self.state.snapshot().get("folders") or {}
        vault = (folders.get(memory_space_id) or {}).get("local_dir")
        if not vault:
            return
        Path(vault).mkdir(parents=True, exist_ok=True)
        registration = register_obsidian_vault(vault)
        if registration is None:
            QDesktopServices.openUrl(
                QUrl(f"obsidian://open?path={quote(vault, safe='')}")
            )
            return

        uri = QUrl(f"obsidian://open?vault={quote(registration.vault_id, safe='')}")
        if registration.added and shutil.which("obsidian"):
            # The running app caches its known-vault registry. This is an explicit
            # Open action, so restart through Obsidian's supported CLI before
            # targeting the newly registered vault.
            try:
                subprocess.Popen(
                    ["obsidian", "restart"],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                QTimer.singleShot(1500, lambda: QDesktopServices.openUrl(uri))
                return
            except OSError:
                pass
        QDesktopServices.openUrl(uri)

    def _choose_folder(self) -> None:
        current = self.state.snapshot()["vault_dir"]
        activate_for_dialog()
        chosen = QFileDialog.getExistingDirectory(
            None, "Choose Chronicle vault", current
        )
        if chosen and self.manager:
            self.manager.set_vault_dir(chosen)

    def _choose_space_folder(self, memory_space_id: str) -> None:
        folders = self.state.snapshot().get("folders") or {}
        current = (folders.get(memory_space_id) or {}).get("local_dir") or ""
        activate_for_dialog()
        chosen = QFileDialog.getExistingDirectory(
            None, "Choose Chronicle memory-space vault", current
        )
        if chosen and self.manager:
            self.manager.set_vault_dir(chosen, memory_space_id)

    def _open_syncthing(self) -> None:
        if self.manager:
            QDesktopServices.openUrl(QUrl(self.manager.syncthing.base_url))

    def shutdown(self) -> None:
        if self.manager:
            self.manager.shutdown()
