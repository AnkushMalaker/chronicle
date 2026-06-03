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
import time
from pathlib import Path
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

APP_SUPPORT = (
    Path.home() / "Library" / "Application Support" / "Chronicle" / "vault-sync"
)
SYNCTHING_HOME = APP_SUPPORT / "syncthing"  # config, keys, index db
APIKEY_FILE = APP_SUPPORT / "apikey"
# Default off 8385 so we don't collide with a user's own Syncthing on 8384.
GUI_PORT = int(os.getenv("VAULT_SYNC_GUI_PORT", "8385"))


def _find_binary() -> str:
    """Locate the syncthing binary, preferring PATH then common Homebrew locations."""
    exe = shutil.which("syncthing")
    if exe:
        return exe
    for candidate in (
        Path("/opt/homebrew/bin/syncthing"),
        Path("/usr/local/bin/syncthing"),
    ):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "syncthing not found. Install it with: brew install syncthing"
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
        """Upsert the vault folder mapped to the local path, shared with the server."""
        Path(path).mkdir(parents=True, exist_ok=True)
        with self._client() as c:
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

    def folder_completion(self, folder_id: str) -> Optional[float]:
        """Return sync completion percentage for the folder, or None if unknown."""
        try:
            with self._client() as c:
                resp = c.get("/rest/db/status", params={"folder": folder_id})
                if resp.status_code != 200:
                    return None
                data = resp.json()
                total = data.get("globalBytes", 0)
                need = data.get("needBytes", 0)
                if total <= 0:
                    return 100.0
                return max(0.0, min(100.0, (1 - need / total) * 100.0))
        except httpx.HTTPError:
            return None
