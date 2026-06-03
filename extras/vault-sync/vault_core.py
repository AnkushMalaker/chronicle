"""Config, backend auth, and pairing-broker calls for the vault-sync app.

The Mac authenticates to Chronicle with its normal JWT and asks the backend's
``/api/vault-sync`` broker to pair it. The broker returns the server's Syncthing
device id + sync address + this user's folder id, which the local Syncthing is then
configured with (see syncthing_manager).
"""

import logging
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Make the repo-root discovery.py importable when running from the repo checkout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Persisted local vault directory (set via the "Choose Vault Folder…" menu item).
APP_SUPPORT = (
    Path.home() / "Library" / "Application Support" / "Chronicle" / "vault-sync"
)
VAULT_DIR_FILE = APP_SUPPORT / "vault_dir.txt"


def _persisted_vault_dir() -> Optional[str]:
    try:
        if VAULT_DIR_FILE.exists():
            return VAULT_DIR_FILE.read_text().strip() or None
    except OSError:
        pass
    return None


def save_vault_dir(path: str) -> None:
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    VAULT_DIR_FILE.write_text(path)


@dataclass
class VaultSyncConfig:
    backend_url: str
    auth_username: str
    auth_password: str
    local_vault_dir: str
    device_name: str

    @classmethod
    def from_env(cls) -> "VaultSyncConfig":
        backend_url = os.getenv("BACKEND_URL")
        if not backend_url:
            try:
                from discovery import CHRONICLE_BACKEND, discover_service

                discovered = discover_service(CHRONICLE_BACKEND)
                if discovered:
                    backend_url = discovered
                    logger.info(
                        "Discovered Chronicle backend via minidisc: %s", discovered
                    )
            except ImportError:
                pass

        vault_dir = (
            _persisted_vault_dir() or os.getenv("LOCAL_VAULT_DIR") or "~/ChronicleVault"
        )

        return cls(
            backend_url=backend_url or "http://localhost:8000",
            auth_username=os.getenv("AUTH_USERNAME") or os.getenv("ADMIN_EMAIL", ""),
            auth_password=os.getenv("AUTH_PASSWORD") or os.getenv("ADMIN_PASSWORD", ""),
            local_vault_dir=os.path.expanduser(vault_dir),
            device_name=os.getenv("DEVICE_NAME") or socket.gethostname(),
        )


def get_jwt_token(username: str, password: str, backend_url: str) -> Optional[str]:
    """Exchange email+password for a JWT, or return None on failure."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{backend_url}/auth/jwt/login",
                data={"username": username, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code == 200:
            return resp.json().get("access_token")
        logger.error("Backend auth failed: HTTP %s", resp.status_code)
    except httpx.HTTPError as e:
        logger.error("Backend auth error: %s", e)
    return None


def broker_pair(backend_url: str, token: str, device_id: str, device_name: str) -> dict:
    """Ask the backend to register this device and share the user's vault folder.

    Returns the broker payload: server_device_id, sync_address, folder_id, folder_label.
    Raises httpx.HTTPStatusError on a non-2xx response.
    """
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{backend_url}/api/vault-sync/pair",
            headers={"Authorization": f"Bearer {token}"},
            json={"device_id": device_id, "device_name": device_name},
        )
    resp.raise_for_status()
    return resp.json()
