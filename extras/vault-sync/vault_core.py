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
    if sys.platform == "darwin"
    else Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
    / "chronicle-vault-sync"
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
    api_key: str
    local_vault_dir: str
    device_name: str

    @classmethod
    def from_env(cls) -> "VaultSyncConfig":
        # Lazy import: sys.path-dependent (repo-root discovery.py, inserted above)
        from discovery import resolve_backend_url

        backend_url = resolve_backend_url(os.getenv("BACKEND_URL"), logger=logger)

        vault_dir = (
            _persisted_vault_dir() or os.getenv("LOCAL_VAULT_DIR") or "~/ChronicleVault"
        )

        return cls(
            backend_url=backend_url,
            api_key=os.getenv("CHRONICLE_API_KEY", ""),
            local_vault_dir=os.path.expanduser(vault_dir),
            device_name=os.getenv("DEVICE_NAME") or socket.gethostname(),
        )


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
