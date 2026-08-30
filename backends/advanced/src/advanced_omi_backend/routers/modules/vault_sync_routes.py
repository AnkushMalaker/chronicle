"""Vault sync pairing broker.

Brokers Syncthing pairing between the server-side Syncthing instance (which shares
each user's Obsidian vault at ``data/conversation_docs/{user_id}``) and the user's Mac.

The Mac app authenticates to Chronicle with its normal JWT, then:
  - ``GET  /api/vault-sync/info``  -> server device id + sync address + this user's folder id
  - ``POST /api/vault-sync/pair``  -> registers the Mac's device id and shares the user's
    vault folder with it on the server Syncthing.

The Mac never needs the server Syncthing API key; the backend drives Syncthing's REST
API over the internal docker network. Syncthing applies REST config changes immediately,
so no restart is needed after pairing.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.memory_space import MemorySpace
from advanced_omi_backend.models.vault_sync import PairRequest
from advanced_omi_backend.services.memory.scope import MemoryScope, MemoryScopeError
from advanced_omi_backend.services.vault_sync import vault_sync_broker
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vault-sync", tags=["vault-sync"])

# Server Syncthing REST API, reachable on the internal docker network.
SYNCTHING_URL = os.getenv("VAULT_SYNC_SYNCTHING_URL", "http://vault-syncthing:8384")
SYNCTHING_API_KEY = os.getenv("VAULT_SYNC_API_KEY", "")
# Externally reachable sync address(es) handed to the Mac — comma-separated, e.g.
# "tcp://my-host.ts.net:22000,tcp://10.0.0.5:22000"; the client dials each
# until one connects (Tailnet when remote, LAN IP when a device has no Tailscale).
# Empty -> the Mac relies on Syncthing's own discovery/relays to find the server.
SYNCTHING_ADDRESS = os.getenv("VAULT_SYNC_ADDRESS", "")

# Obsidian's per-vault workspace/config dir is local state, not content worth
# syncing. Ignore it and everything under it on every paired device.
_VAULT_IGNORE_PATTERNS = [".obsidian", ".obsidian/**"]

# conversation_docs is mounted at /vaults inside the Syncthing container.
_SYNCTHING_VAULTS_DIR = "/vaults"
# ...and at /app/data/conversation_docs inside the backend container (same host dir).
_BACKEND_VAULTS_DIR = Path(os.getenv("DATA_DIR", "/app/data")) / "conversation_docs"


def _folder_id(user_id: str) -> str:
    return f"vault-{user_id}"


def _client() -> httpx.AsyncClient:
    if not SYNCTHING_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Vault sync is not configured (VAULT_SYNC_API_KEY unset)",
        )
    return httpx.AsyncClient(
        base_url=SYNCTHING_URL,
        headers={"X-API-Key": SYNCTHING_API_KEY},
        timeout=10.0,
    )


async def _server_device_id(client: httpx.AsyncClient) -> str:
    resp = await client.get("/rest/system/status")
    resp.raise_for_status()
    return resp.json()["myID"]


@router.get("/info")
async def vault_sync_info(
    memory_space_id: Optional[str] = None,
    current_user: User = Depends(current_active_user),
):
    """Return what the Mac needs to dial and identify the server's vault folder."""
    if memory_space_id:
        scope = MemoryScope(str(current_user.user_id), memory_space_id)
        try:
            space = await vault_sync_broker.resolver.require_space(scope)
            return await vault_sync_broker.info(scope, space_name=space.name)
        except MemoryScopeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    async with _client() as client:
        try:
            server_device_id = await _server_device_id(client)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=503, detail=f"Syncthing unreachable: {e}")

    return {
        "server_device_id": server_device_id,
        "sync_address": SYNCTHING_ADDRESS,
        "folder_id": _folder_id(current_user.user_id),
        "folder_label": "Chronicle Vault",
    }


@router.post("/pair")
async def vault_sync_pair(
    req: PairRequest, current_user: User = Depends(current_active_user)
):
    """Register the Mac device and share this user's vault folder with it.

    Idempotent: re-pairing the same device is a no-op; pairing a second Mac adds it to
    the existing folder rather than replacing the first.
    """
    user_id = current_user.user_id
    if req.memory_space_id:
        scope = MemoryScope(str(user_id), req.memory_space_id)
        try:
            space = await vault_sync_broker.resolver.require_space(scope, writable=True)
            result = await vault_sync_broker.pair(
                scope,
                device_id=req.device_id,
                device_name=req.device_name,
                space_name=space.name,
            )
            space.sync_state = "syncing"
            space.sync_error = None
            await space.save()
            return result
        except MemoryScopeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    folder_id = _folder_id(user_id)

    # Ensure the per-user vault dir AND Syncthing's folder marker exist so Syncthing
    # has a valid folder path. Same host dir is visible to Syncthing at /vaults/{user_id}.
    # Syncthing auto-creates the .stfolder marker only when a folder is first added; if the
    # vault dir is later recreated (e.g. a data reset wipes conversation_docs/{user_id}) the
    # marker is lost and Syncthing refuses to scan it ("folder marker missing"), silently
    # freezing sync at the last index. Re-asserting it here — co-located with the dir mkdir,
    # at the one point we set the folder up for Syncthing — keeps pairing self-healing.
    vault_dir = _BACKEND_VAULTS_DIR / Path(user_id).name
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / ".stfolder").mkdir(exist_ok=True)
    folder_path = f"{_SYNCTHING_VAULTS_DIR}/{Path(user_id).name}"

    async with _client() as client:
        try:
            server_device_id = await _server_device_id(client)
            await _ensure_device(client, req.device_id, req.device_name)
            await _ensure_folder(
                client, folder_id, folder_path, req.device_id, server_device_id
            )
        except httpx.HTTPStatusError as e:
            logger.error("Syncthing config error: %s -> %s", e, e.response.text)
            raise HTTPException(status_code=502, detail=f"Syncthing config failed: {e}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=503, detail=f"Syncthing unreachable: {e}")

    logger.info(
        "Paired vault device %s (%s) to folder %s",
        req.device_name,
        req.device_id[:7],
        folder_id,
    )
    return {
        "server_device_id": server_device_id,
        "sync_address": SYNCTHING_ADDRESS,
        "folder_id": folder_id,
        "folder_label": "Chronicle Vault",
    }


@router.get("/folders")
async def vault_sync_folders(current_user: User = Depends(current_active_user)):
    """List Main and every owned scoped folder for tray discovery."""
    user_id = str(current_user.user_id)
    spaces = (
        await MemorySpace.find(MemorySpace.user_id == user_id)
        .sort("-updated_at")
        .to_list()
    )
    main = vault_sync_broker.folder(MemoryScope(user_id))
    return {
        "folders": [
            {
                "memory_space_id": None,
                "name": "Main",
                "state": "active",
                "sync_state": "unknown",
                "folder_id": main.folder_id,
                "folder_label": main.label,
            },
            *[
                {
                    "memory_space_id": space.space_id,
                    "name": space.name,
                    "state": space.state,
                    "sync_state": space.sync_state,
                    "folder_id": vault_sync_broker.folder(
                        MemoryScope(user_id, space.space_id), space_name=space.name
                    ).folder_id,
                    "folder_label": vault_sync_broker.folder(
                        MemoryScope(user_id, space.space_id), space_name=space.name
                    ).label,
                }
                for space in spaces
            ],
        ],
    }


async def _ensure_device(client: httpx.AsyncClient, device_id: str, name: str) -> None:
    """Upsert the Mac device into the server Syncthing config."""
    existing = await client.get(f"/rest/config/devices/{device_id}")
    if existing.status_code == 200:
        return
    template = await client.get("/rest/config/defaults/device")
    template.raise_for_status()
    device = template.json()
    device["deviceID"] = device_id
    device["name"] = name
    resp = await client.put(f"/rest/config/devices/{device_id}", json=device)
    resp.raise_for_status()


async def _ensure_folder(
    client: httpx.AsyncClient,
    folder_id: str,
    folder_path: str,
    mac_device_id: str,
    server_device_id: str,
) -> None:
    """Upsert the user's vault folder and make sure both devices share it."""
    existing = await client.get(f"/rest/config/folders/{folder_id}")
    if existing.status_code == 200:
        folder = existing.json()
    else:
        template = await client.get("/rest/config/defaults/folder")
        template.raise_for_status()
        folder = template.json()
        folder["id"] = folder_id
        folder["label"] = "Chronicle Vault"
        folder["path"] = folder_path
        folder["type"] = "sendreceive"

    shared = {d.get("deviceID") for d in folder.get("devices", [])}
    for dev_id in (server_device_id, mac_device_id):
        if dev_id not in shared:
            folder.setdefault("devices", []).append({"deviceID": dev_id})

    resp = await client.put(f"/rest/config/folders/{folder_id}", json=folder)
    resp.raise_for_status()

    ignores = await client.post(
        "/rest/db/ignores",
        params={"folder": folder_id},
        json={"ignore": _VAULT_IGNORE_PATTERNS},
    )
    ignores.raise_for_status()

    # A re-pair with unchanged config won't restart the folder runner, so a folder that
    # was stuck in "folder marker missing" error state (marker now recreated above) won't
    # recover until the periodic rescan. Trigger a scan so pairing recovers it immediately.
    scan = await client.post("/rest/db/scan", params={"folder": folder_id})
    if scan.status_code >= 400:
        logger.warning(
            "Vault folder %s rescan after pair returned %s: %s",
            folder_id,
            scan.status_code,
            scan.text,
        )
