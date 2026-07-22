"""Platform-independent state, logging, and vault-sync orchestration."""

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import httpx

from syncthing_manager import SyncthingManager
from vault_core import VaultSyncConfig, broker_pair, get_jwt_token, save_vault_dir

logger = logging.getLogger(__name__)


class MemoryLogHandler(logging.Handler):
    """Keep recent application log lines for display by either desktop UI."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


log_buffer = MemoryLogHandler()


def configure_logging() -> None:
    """Configure the shared desktop log buffer without adding it twice."""
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(format=log_format, level=logging.INFO)
    log_buffer.setFormatter(logging.Formatter(log_format))
    root = logging.getLogger()
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass
class SharedState:
    """Thread-safe state shared between a platform UI and its worker thread."""

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
    """Coordinate local Syncthing and the Chronicle pairing handshake."""

    def __init__(self, state: SharedState) -> None:
        self.state = state
        self.config = VaultSyncConfig.from_env()
        self.syncthing = SyncthingManager()
        self.state.update(vault_dir=self.config.local_vault_dir)
        self._lock = threading.Lock()
        self._logged_errors: set[str] = set()

    def pair_async(self) -> None:
        threading.Thread(target=self._pair, daemon=True).start()

    def _pair(self) -> None:
        if not self._lock.acquire(blocking=False):
            logger.info("Pair already in progress")
            return
        try:
            cfg = self.config
            if not cfg.auth_username or not cfg.auth_password:
                error = (
                    "Chronicle login is not configured. Set AUTH_USERNAME and "
                    "AUTH_PASSWORD (or ADMIN_EMAIL and ADMIN_PASSWORD) in "
                    f"{VaultSyncConfig.root_env_file()} or "
                    f"{VaultSyncConfig.local_env_file()}, then restart the desktop service."
                )
                logger.error(error)
                self.state.update(
                    status="error", error=error
                )
                return
            self.state.update(status="starting", error=None)
            self.syncthing.start()
            local_id = self.syncthing.device_id()
            self.state.update(status="pairing")
            token = get_jwt_token(cfg.auth_username, cfg.auth_password, cfg.backend_url)
            if not token:
                error = (
                    f"Chronicle login failed for {cfg.auth_username} at {cfg.backend_url}. "
                    "Check the credentials in the configured .env file."
                )
                logger.error(error)
                self.state.update(status="error", error=error)
                return
            info = broker_pair(cfg.backend_url, token, local_id, cfg.device_name)
            sync_address = info.get("sync_address") or ""
            self.syncthing.ensure_server_device(
                info["server_device_id"],
                "Chronicle Server",
                [sync_address] if sync_address else ["dynamic"],
            )
            self.syncthing.ensure_folder(
                folder_id=info["folder_id"],
                path=cfg.local_vault_dir,
                label=info.get("folder_label", "Chronicle Vault"),
                server_device_id=info["server_device_id"],
                self_device_id=local_id,
            )
            self.state.update(status="syncing", folder_id=info["folder_id"], error=None)
            logger.info("Paired. Folder %s -> %s", info["folder_id"], cfg.local_vault_dir)
        except httpx.HTTPStatusError as error:
            detail = error.response.text[:200]
            self.state.update(status="error", error=f"Pair failed: {detail}")
            logger.error("Pair failed: %s", detail)
        except Exception as error:  # noqa: BLE001 - the desktop UI must surface it
            self.state.update(status="error", error=str(error))
            logger.exception("Pair error")
        finally:
            self._lock.release()

    def set_vault_dir(self, path: str) -> None:
        save_vault_dir(path)
        self.config.local_vault_dir = path
        self.state.update(vault_dir=path)
        logger.info("Vault folder set to %s — re-pairing", path)
        self.pair_async()

    def refresh_status(self) -> None:
        if not self.syncthing.is_running():
            return
        snap = self.state.snapshot()
        completion = None
        folder_error = None
        if snap["folder_id"]:
            folder = self.syncthing.folder_status(snap["folder_id"])
            completion = folder["completion"]
            folder_error = folder["error"]

        detailed = self.syncthing.collect_errors()
        for message in detailed:
            if message not in self._logged_errors:
                logger.error("Syncthing: %s", message)
        self._logged_errors = set(detailed)
        if detailed:
            folder_error = (
                detailed[0]
                if len(detailed) == 1
                else f"{detailed[0]} (+{len(detailed) - 1} more)"
            )
        self.state.update(
            connected=self.syncthing.connection_count() > 0,
            completion=completion,
            folder_error=folder_error,
        )

    def shutdown(self) -> None:
        self.syncthing.stop()
