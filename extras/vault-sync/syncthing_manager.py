"""Manage a local headless Syncthing instance on the Mac and drive its REST API.

The menu bar app owns a private Syncthing process (separate home dir + GUI port from
any Syncthing the user may already run), and configures it entirely over REST so the
user never touches the Syncthing UI. Pairing with the server is brokered by the
Chronicle backend; this module only handles the local side.
"""

import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

APP_SUPPORT = (
    Path.home() / "Library" / "Application Support" / "Chronicle" / "vault-sync"
    if sys.platform == "darwin"
    else Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
    / "chronicle-vault-sync"
)
SYNCTHING_HOME = APP_SUPPORT / "syncthing"  # config, keys, index db
APIKEY_FILE = APP_SUPPORT / "apikey"
# Default off 8385 so we don't collide with a user's own Syncthing on 8384.
GUI_PORT = int(os.getenv("VAULT_SYNC_GUI_PORT", "8385"))

# Obsidian's per-vault workspace/config dir is local state, not content worth
# syncing. Ignore it and everything under it on every paired device.
VAULT_IGNORE_PATTERNS = [".obsidian", ".obsidian/**"]


def _find_binary() -> str:
    """Locate the syncthing binary, preferring PATH then common install locations."""
    exe = shutil.which("syncthing")
    if exe:
        return exe
    for candidate in (
        Path("/opt/homebrew/bin/syncthing"),
        Path("/usr/local/bin/syncthing"),
        Path("/usr/bin/syncthing"),
    ):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "syncthing not found. Install it with your system package manager"
    )


def _api_key() -> str:
    """Return a persistent local API key, generating one on first run."""
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    if APIKEY_FILE.exists():
        return APIKEY_FILE.read_text().strip()
    key = secrets.token_hex(24)
    APIKEY_FILE.write_text(key)
    APIKEY_FILE.chmod(0o600)
    return key


class SyncthingManager:
    """Owns the local Syncthing subprocess and its REST configuration."""

    def __init__(self) -> None:
        self.binary = _find_binary()
        self.api_key = _api_key()
        self.base_url = f"http://127.0.0.1:{GUI_PORT}"
        self._proc: Optional[subprocess.Popen] = None
        self._log = None

    # --- process lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self.is_running():
            return
        SYNCTHING_HOME.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, STGUIAPIKEY=self.api_key)
        # Capture output so a failed launch (e.g. bad flag) surfaces in the error
        # instead of hiding behind a generic "did not become ready" timeout.
        self._log = open(APP_SUPPORT / "syncthing.log", "ab")
        self._proc = subprocess.Popen(
            [
                self.binary,
                "serve",
                "--home",
                str(SYNCTHING_HOME),
                "--gui-address",
                f"127.0.0.1:{GUI_PORT}",
                "--gui-apikey",
                self.api_key,
                "--no-browser",
            ],
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        self._wait_ready()
        logger.info("Local Syncthing ready on %s", self.base_url)

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise RuntimeError(
                    f"Syncthing exited early (code {self._proc.returncode}). "
                    f"See {APP_SUPPORT / 'syncthing.log'}"
                )
            try:
                with self._client() as c:
                    if c.get("/rest/system/ping").status_code == 200:
                        return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Local Syncthing did not become ready in time")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": self.api_key},
            timeout=10.0,
        )

    # --- REST operations -----------------------------------------------------

    def device_id(self) -> str:
        with self._client() as c:
            resp = c.get("/rest/system/status")
            resp.raise_for_status()
            return resp.json()["myID"]

    def ensure_server_device(
        self, device_id: str, name: str, addresses: List[str]
    ) -> None:
        """Upsert the Chronicle server as a Syncthing device to dial."""
        with self._client() as c:
            existing = c.get(f"/rest/config/devices/{device_id}")
            if existing.status_code == 200:
                device = existing.json()
            else:
                template = c.get("/rest/config/defaults/device")
                template.raise_for_status()
                device = template.json()
                device["deviceID"] = device_id
            device["name"] = name
            device["addresses"] = addresses or ["dynamic"]
            resp = c.put(f"/rest/config/devices/{device_id}", json=device)
            resp.raise_for_status()

    def ensure_folder(
        self,
        folder_id: str,
        path: str,
        label: str,
        server_device_id: str,
        self_device_id: str,
    ) -> None:
        """Upsert the vault folder mapped to the local path, shared with the server.

        Any *other* folder already bound to the same local path is detached first.
        Syncthing refuses to run two folders that share a directory, so a leftover
        folder from a previous pairing would silently wedge sync. This happens when
        the backend re-creates the admin user with a new id (e.g. after a data
        reset): ``folder_id`` is ``vault-{user_id}``, so a new user id means a new
        folder pointed at the same local vault dir. Detaching the stale one lets the
        current vault take over the path cleanly.
        """
        Path(path).mkdir(parents=True, exist_ok=True)
        target = os.path.realpath(path)
        with self._client() as c:
            self._detach_folders_at_path(c, target, keep_id=folder_id)
            existing = c.get(f"/rest/config/folders/{folder_id}")
            if existing.status_code == 200:
                folder = existing.json()
            else:
                template = c.get("/rest/config/defaults/folder")
                template.raise_for_status()
                folder = template.json()
                folder["id"] = folder_id
                folder["type"] = "sendreceive"
            folder["label"] = label
            folder["path"] = path
            shared = {d.get("deviceID") for d in folder.get("devices", [])}
            for dev in (self_device_id, server_device_id):
                if dev not in shared:
                    folder.setdefault("devices", []).append({"deviceID": dev})
            resp = c.put(f"/rest/config/folders/{folder_id}", json=folder)
            resp.raise_for_status()
            self._set_ignores(c, folder_id, VAULT_IGNORE_PATTERNS)

    @staticmethod
    def _detach_folders_at_path(c: httpx.Client, target: str, keep_id: str) -> None:
        """Remove any folder bound to ``target`` whose id differs from ``keep_id``."""
        resp = c.get("/rest/config/folders")
        resp.raise_for_status()
        for folder in resp.json():
            fid = folder.get("id")
            if not fid or fid == keep_id:
                continue
            if os.path.realpath(folder.get("path", "")) == target:
                logger.info("Detaching stale folder %s bound to %s", fid, target)
                c.delete(f"/rest/config/folders/{fid}").raise_for_status()

    @staticmethod
    def _set_ignores(c: httpx.Client, folder_id: str, patterns: list[str]) -> None:
        """Write the folder's .stignore patterns via REST."""
        resp = c.post(
            "/rest/db/ignores",
            params={"folder": folder_id},
            json={"ignore": patterns},
        )
        resp.raise_for_status()

    def connection_count(self) -> int:
        """Number of currently connected remote devices (0 or 1 in normal use)."""
        try:
            with self._client() as c:
                resp = c.get("/rest/system/connections")
                if resp.status_code != 200:
                    return 0
                conns = resp.json().get("connections", {})
                return sum(1 for v in conns.values() if v.get("connected"))
        except httpx.HTTPError:
            return 0

    def folder_status(self, folder_id: str) -> dict:
        """Return ``{"completion", "state", "error"}`` for the folder.

        ``completion`` is the percent of bytes in sync (None if unknown). An empty
        folder only reads as 100% when Syncthing also reports it idle with no error:
        a wedged folder (overlapping path, missing marker, scan failure) surfaces its
        error instead of a misleading 100%, which previously made a broken sync show
        as "In sync".
        """
        unknown = {"completion": None, "state": None, "error": None}
        try:
            with self._client() as c:
                resp = c.get("/rest/db/status", params={"folder": folder_id})
                if resp.status_code != 200:
                    return unknown
                data = resp.json()
                state = data.get("state")
                error = data.get("error") or None
                if not error and data.get("errors"):
                    error = f"{data['errors']} item error(s)"
                total = data.get("globalBytes", 0)
                need = data.get("needBytes", 0)
                if total <= 0:
                    completion = (
                        100.0 if state in ("idle", None) and not error else None
                    )
                else:
                    completion = max(0.0, min(100.0, (1 - need / total) * 100.0))
                return {"completion": completion, "state": state, "error": error}
        except httpx.HTTPError:
            return unknown

    def collect_errors(self) -> List[str]:
        """Return detailed Syncthing-side errors (system + per-folder pull failures).

        ``folder_status`` only reports a count ("N item error(s)"); the actual
        messages — case collisions, permission denials, failed pulls — live in
        ``/rest/folder/errors`` and ``/rest/system/error``. The menu polls this so
        the real cause reaches "View Logs" instead of staying buried in Syncthing.
        """
        messages: List[str] = []
        try:
            with self._client() as c:
                sys_resp = c.get("/rest/system/error")
                if sys_resp.status_code == 200:
                    for e in sys_resp.json().get("errors") or []:
                        messages.append(f"system: {e.get('message', e)}")

                folders = c.get("/rest/config/folders")
                if folders.status_code != 200:
                    return messages
                for f in folders.json():
                    fid = f["id"]
                    label = f.get("label") or fid
                    resp = c.get("/rest/folder/errors", params={"folder": fid})
                    if resp.status_code != 200:
                        continue
                    for e in resp.json().get("errors") or []:
                        messages.append(f"{label}: {e.get('path')}: {e.get('error')}")
        except httpx.HTTPError as e:
            logger.debug("Could not fetch Syncthing errors: %s", e)
        return messages
