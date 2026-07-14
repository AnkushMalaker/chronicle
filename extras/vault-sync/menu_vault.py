"""macOS menu bar app for Chronicle vault sync.

Owns a local headless Syncthing, pairs it with the Chronicle server via the backend
broker, and keeps the user's Obsidian vault folder in sync. No terminal or Syncthing
UI needed — just pick a folder and open it in Obsidian.
"""

import logging
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
import rumps
from dotenv import load_dotenv
from syncthing_manager import SyncthingManager
from vault_core import VaultSyncConfig, broker_pair, get_jwt_token, save_vault_dir

logger = logging.getLogger(__name__)

load_dotenv()


class MemoryLogHandler(logging.Handler):
    """Keep recent log lines in memory for display in the menu bar UI."""

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
    scroll.setBorderType_(NSBezelBorder)

    content = scroll.contentSize()
    text_view = NSTextView.alloc().initWithFrame_(
        NSMakeRect(0, 0, content.width, content.height)
    )
    text_view.setMinSize_(NSMakeSize(0, content.height))
    text_view.setMaxSize_(NSMakeSize(1e7, 1e7))
    text_view.setVerticallyResizable_(True)
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


def _choose_directory(default_path: str) -> Optional[str]:
    """Open a native folder picker; return the chosen absolute path or None."""
    # Lazy import: macOS-only (AppKit, not available cross-platform)
    from AppKit import NSURL, NSOpenPanel

    panel = NSOpenPanel.openPanel()
    panel.setCanChooseFiles_(False)
    panel.setCanChooseDirectories_(True)
    panel.setCanCreateDirectories_(True)
    panel.setPrompt_("Select Vault")
    parent = Path(default_path).expanduser().parent
    if parent.exists():
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(parent)))
    if panel.runModal() == 1:  # NSModalResponseOK
        return panel.URLs()[0].path()
    return None


# --- shared state ------------------------------------------------------------


@dataclass
class SharedState:
    """Thread-safe state shared between the rumps UI and the worker thread."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    status: str = "idle"  # idle | starting | pairing | syncing | error
    error: Optional[str] = None
    connected: bool = False
    completion: Optional[float] = None
    folder_error: Optional[str] = None  # folder-level error from Syncthing
    folder_id: Optional[str] = None
    vault_dir: str = ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "error": self.error,
                "connected": self.connected,
                "completion": self.completion,
                "folder_error": self.folder_error,
                "folder_id": self.folder_id,
                "vault_dir": self.vault_dir,
            }

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)


# --- sync manager ------------------------------------------------------------


class VaultSyncManager:
    """Coordinates the local Syncthing and the backend pairing handshake."""

    def __init__(self, state: SharedState) -> None:
        self.state = state
        self.config = VaultSyncConfig.from_env()
        self.syncthing = SyncthingManager()
        self.state.update(vault_dir=self.config.local_vault_dir)
        self._lock = threading.Lock()
        self._logged_errors: set[str] = set()

    def pair_async(self) -> None:
        """Run the (blocking) pair flow on a background thread."""
        threading.Thread(target=self._pair, daemon=True).start()

    def _pair(self) -> None:
        if not self._lock.acquire(blocking=False):
            logger.info("Pair already in progress")
            return
        try:
            cfg = self.config
            if not cfg.auth_username or not cfg.auth_password:
                self.state.update(
                    status="error",
                    error="Set AUTH_USERNAME/AUTH_PASSWORD in .env",
                )
                return

            self.state.update(status="starting", error=None)
            self.syncthing.start()
            local_id = self.syncthing.device_id()

            self.state.update(status="pairing")
            token = get_jwt_token(cfg.auth_username, cfg.auth_password, cfg.backend_url)
            if not token:
                self.state.update(status="error", error="Backend auth failed")
                return

            info = broker_pair(cfg.backend_url, token, local_id, cfg.device_name)

            sync_address = info.get("sync_address") or ""
            addresses = [sync_address] if sync_address else ["dynamic"]
            self.syncthing.ensure_server_device(
                info["server_device_id"], "Chronicle Server", addresses
            )
            self.syncthing.ensure_folder(
                folder_id=info["folder_id"],
                path=cfg.local_vault_dir,
                label=info.get("folder_label", "Chronicle Vault"),
                server_device_id=info["server_device_id"],
                self_device_id=local_id,
            )
            self.state.update(status="syncing", folder_id=info["folder_id"], error=None)
            logger.info(
                "Paired. Folder %s -> %s", info["folder_id"], cfg.local_vault_dir
            )
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:200] if e.response is not None else str(e)
            self.state.update(status="error", error=f"Pair failed: {detail}")
            logger.error("Pair failed: %s", detail)
        except Exception as e:  # noqa: BLE001 - surface any failure in the UI
            self.state.update(status="error", error=str(e))
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
        connected = self.syncthing.connection_count() > 0
        completion = None
        folder_error = None
        snap = self.state.snapshot()
        if snap["folder_id"]:
            fstatus = self.syncthing.folder_status(snap["folder_id"])
            completion = fstatus["completion"]
            folder_error = fstatus["error"]

        # Pull Syncthing's own detailed errors into the log buffer so "View Logs"
        # shows the real cause (e.g. a case collision), not just a count. Log each
        # message once; re-log if it clears and recurs.
        detailed = self.syncthing.collect_errors()
        for msg in detailed:
            if msg not in self._logged_errors:
                logger.error("Syncthing: %s", msg)
        self._logged_errors = set(detailed)
        if detailed:
            folder_error = (
                detailed[0]
                if len(detailed) == 1
                else f"{detailed[0]} (+{len(detailed) - 1} more)"
            )

        self.state.update(
            connected=connected, completion=completion, folder_error=folder_error
        )

    def shutdown(self) -> None:
        self.syncthing.stop()


# --- rumps menu bar app -------------------------------------------------------


class VaultSyncApp(rumps.App):
    def __init__(self, state: SharedState, manager: VaultSyncManager) -> None:
        super().__init__("Chronicle Vault", title="◈…")  # ◈
        self.state = state
        self.manager = manager

        self.status_item = rumps.MenuItem("Status: Starting…", callback=None)
        self.conn_item = rumps.MenuItem("Server: …", callback=None)
        self.folder_item = rumps.MenuItem("Vault: …", callback=None)
        self.menu = [
            self.status_item,
            self.conn_item,
            self.folder_item,
            None,
            rumps.MenuItem("Open in Obsidian", callback=self.on_open_obsidian),
            rumps.MenuItem("Choose Vault Folder…", callback=self.on_choose_folder),
            rumps.MenuItem("Sync Now / Re-pair", callback=self.on_repair),
            None,
            rumps.MenuItem("Open Syncthing UI", callback=self.on_open_syncthing),
            rumps.MenuItem("View Logs", callback=self.on_view_logs),
        ]

    @rumps.timer(2)
    def refresh_ui(self, _sender) -> None:
        self.manager.refresh_status()
        snap = self.state.snapshot()
        status = snap["status"]
        self.folder_item.title = f"Vault: {snap['vault_dir']}"

        if status == "error":
            self.title = "◈!"  # ◈!
            self.status_item.title = f"Status: Error — {snap['error'] or 'unknown'}"
            self.conn_item.title = "Server: —"
            return

        if status == "syncing":
            connected = snap["connected"]
            self.conn_item.title = (
                "Server: connected" if connected else "Server: connecting…"
            )
            folder_error = snap["folder_error"]
            comp = snap["completion"]
            if folder_error:
                self.title = "◈!"  # ◈!
                # Truncate for the menu; the full error is in the log buffer (View Logs).
                short = (
                    folder_error if len(folder_error) <= 80 else folder_error[:79] + "…"
                )
                self.status_item.title = f"Status: Folder error — {short}"
            elif comp is not None and comp >= 99.9 and connected:
                self.title = "◈✓"  # ◈✓
                self.status_item.title = "Status: In sync"
            else:
                self.title = "◈↻"  # ◈↻
                pct = f"{comp:.0f}%" if comp is not None else "…"
                self.status_item.title = f"Status: Syncing {pct}"
        elif status in ("starting", "pairing"):
            self.title = "◈…"  # ◈…
            self.status_item.title = f"Status: {status.capitalize()}…"
            self.conn_item.title = "Server: —"
        else:
            self.title = "◈"
            self.status_item.title = "Status: Idle"

    def on_open_obsidian(self, _sender) -> None:
        vault_dir = self.state.snapshot()["vault_dir"]
        uri = f"obsidian://open?path={quote(vault_dir)}"
        try:
            subprocess.run(["open", uri], check=False)
            logger.info("Opened %s in Obsidian", vault_dir)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to open Obsidian: %s", e)
            subprocess.run(["open", vault_dir], check=False)  # reveal in Finder

    def on_choose_folder(self, _sender) -> None:
        current = self.state.snapshot()["vault_dir"]
        chosen = _choose_directory(current)
        if chosen:
            self.manager.set_vault_dir(chosen)

    def on_repair(self, _sender) -> None:
        logger.info("User requested re-pair")
        self.manager.pair_async()

    def on_open_syncthing(self, _sender) -> None:
        url = self.manager.syncthing.base_url
        logger.info("Opening Syncthing UI at %s", url)
        subprocess.run(["open", url], check=False)

    def on_view_logs(self, _sender) -> None:
        _show_logs_dialog("Chronicle Vault Sync — Logs", list(log_buffer.lines))


# --- entry point --------------------------------------------------------------


def main() -> None:
    # Lazy import: macOS-only (AppKit, not available cross-platform)
    from AppKit import NSApplication

    NSApplication.sharedApplication().setActivationPolicy_(1)  # menu bar only

    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(format=log_format, level=logging.INFO)
    log_buffer.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(log_buffer)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    state = SharedState()
    manager = VaultSyncManager(state)
    manager.pair_async()  # auto-start sync on launch

    app = VaultSyncApp(state, manager)
    try:
        app.run()
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
