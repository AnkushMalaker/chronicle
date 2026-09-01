"""Config and pairing-broker calls for the vault-sync app.

This machine authenticates to Chronicle with a long-lived API key and asks the
backend's ``/api/vault-sync`` broker to pair it. The broker returns the server's
Syncthing device id + sync address + this user's folder id, which the local
Syncthing is then configured with (see :mod:`chronicle_vault_sync.syncthing`).
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from chronicle_client import ClientConfig, auth_headers

logger = logging.getLogger(__name__)

# Persisted local vault directory (set via the "Choose Vault Folder…" menu item).
APP_SUPPORT = (
    Path.home() / "Library" / "Application Support" / "Chronicle" / "vault-sync"
    if sys.platform == "darwin"
    else Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
    / "chronicle-vault-sync"
)
VAULT_DIR_FILE = APP_SUPPORT / "vault_dir.txt"


def _vault_dir_file(memory_space_id: Optional[str] = None) -> Path:
    if memory_space_id is None:
        return VAULT_DIR_FILE
    safe_id = Path(memory_space_id).name
    if safe_id != memory_space_id or safe_id in {"", ".", ".."}:
        raise ValueError("Invalid memory space id")
    return APP_SUPPORT / f"vault_dir.{safe_id}.txt"


def persisted_vault_dir(memory_space_id: Optional[str] = None) -> Optional[str]:
    try:
        path = _vault_dir_file(memory_space_id)
        if path.exists():
            return path.read_text().strip() or None
    except OSError:
        pass
    return None


def save_vault_dir(path: str, memory_space_id: Optional[str] = None) -> None:
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    _vault_dir_file(memory_space_id).write_text(path)


@dataclass
class VaultSyncConfig:
    backend_url: str
    api_key: str
    local_vault_dir: str
    device_name: str

    @classmethod
    def from_env(cls) -> "VaultSyncConfig":
        client = ClientConfig.from_env()
        vault_dir = (
            persisted_vault_dir() or os.getenv("LOCAL_VAULT_DIR") or "~/ChronicleVault"
        )
        return cls(
            backend_url=client.backend_url,
            api_key=client.api_key,
            local_vault_dir=os.path.expanduser(vault_dir),
            device_name=client.device_name,
        )


def broker_pair(
    backend_url: str,
    token: str,
    device_id: str,
    device_name: str,
    memory_space_id: Optional[str] = None,
) -> dict:
    """Ask the backend to register this device and share the user's vault folder.

    Returns the broker payload: server_device_id, sync_address, folder_id, folder_label.
    Raises httpx.HTTPStatusError on a non-2xx response.
    """
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{backend_url}/api/vault-sync/pair",
            headers=auth_headers(token),
            json={
                "device_id": device_id,
                "device_name": device_name,
                "memory_space_id": memory_space_id,
            },
        )
    resp.raise_for_status()
    return resp.json()


def broker_folders(backend_url: str, token: str) -> list[dict]:
    """Return every Main/space folder the authenticated user may pair."""
    with httpx.Client(timeout=15.0) as client:
        response = client.get(
            f"{backend_url}/api/vault-sync/folders",
            headers=auth_headers(token),
        )
    response.raise_for_status()
    return list(response.json().get("folders") or [])


def broker_space_action(
    backend_url: str,
    token: str,
    memory_space_id: str,
    action: str,
) -> dict:
    """Apply an authenticated lifecycle action to one owned scoped vault."""

    endpoints = {
        "freeze": f"/api/spaces/{memory_space_id}/sync/freeze",
        "rescan": f"/api/spaces/{memory_space_id}/sync/rescan",
        "resume": f"/api/spaces/{memory_space_id}/sync/resume",
        "reopen": f"/api/spaces/{memory_space_id}/reopen",
    }
    try:
        endpoint = endpoints[action]
    except KeyError as exc:
        raise ValueError(f"Unsupported memory-space action: {action}") from exc
    with httpx.Client(timeout=15.0) as client:
        response = client.post(
            f"{backend_url}{endpoint}",
            headers=auth_headers(token),
        )
    response.raise_for_status()
    return response.json()
