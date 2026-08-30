"""Scoped Syncthing folder operations shared by HTTP and memory-space merges."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from advanced_omi_backend.services.memory.scope import (
    MemoryScope,
    MemoryScopeError,
    MemoryScopeResolver,
)


class VaultSyncUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ScopedSyncFolder:
    folder_id: str
    label: str
    backend_path: Path
    syncthing_path: str


class VaultSyncBroker:
    def __init__(self, resolver: Optional[MemoryScopeResolver] = None):
        self.resolver = resolver or MemoryScopeResolver()
        self.url = os.getenv("VAULT_SYNC_SYNCTHING_URL", "http://vault-syncthing:8384")
        self.api_key = os.getenv("VAULT_SYNC_API_KEY", "")
        self.address = os.getenv("VAULT_SYNC_ADDRESS", "")

    def folder(
        self, scope: MemoryScope, *, space_name: Optional[str] = None
    ) -> ScopedSyncFolder:
        if scope.is_main:
            user_id = self.resolver._safe_component(scope.user_id, "user_id")
            return ScopedSyncFolder(
                folder_id=f"vault-{user_id}",
                label="Chronicle Vault",
                backend_path=self.resolver.main_root(user_id),
                syncthing_path=f"/vaults/{user_id}",
            )
        space_id = self.resolver._space_component(scope.memory_space_id)
        user_id = self.resolver._safe_component(scope.user_id, "user_id")
        return ScopedSyncFolder(
            folder_id=f"space-{user_id}-{space_id}",
            label=f"Chronicle Space — {space_name or space_id}",
            backend_path=self.resolver.vault_root(scope),
            syncthing_path=f"/memory-spaces/{user_id}/{space_id}/vault",
        )

    def _client(self) -> httpx.AsyncClient:
        if not self.api_key:
            raise VaultSyncUnavailable("Vault sync is not configured")
        return httpx.AsyncClient(
            base_url=self.url,
            headers={"X-API-Key": self.api_key},
            timeout=10.0,
        )

    @staticmethod
    async def _server_device_id(client: httpx.AsyncClient) -> str:
        response = await client.get("/rest/system/status")
        response.raise_for_status()
        return response.json()["myID"]

    async def info(
        self, scope: MemoryScope, *, space_name: Optional[str] = None
    ) -> dict:
        folder = self.folder(scope, space_name=space_name)
        async with self._client() as client:
            server_device_id = await self._server_device_id(client)
        return {
            "server_device_id": server_device_id,
            "sync_address": self.address,
            "folder_id": folder.folder_id,
            "folder_label": folder.label,
            "memory_space_id": scope.memory_space_id,
        }

    async def pair(
        self,
        scope: MemoryScope,
        *,
        device_id: str,
        device_name: str,
        space_name: Optional[str] = None,
    ) -> dict:
        folder = self.folder(scope, space_name=space_name)
        folder.backend_path.mkdir(parents=True, exist_ok=True)
        (folder.backend_path / ".stfolder").mkdir(exist_ok=True)
        async with self._client() as client:
            server_device_id = await self._server_device_id(client)
            await self._ensure_device(client, device_id, device_name)
            await self._ensure_folder(
                client, folder, device_id=device_id, server_device_id=server_device_id
            )
        return {
            "server_device_id": server_device_id,
            "sync_address": self.address,
            "folder_id": folder.folder_id,
            "folder_label": folder.label,
            "memory_space_id": scope.memory_space_id,
        }

    async def _ensure_device(
        self, client: httpx.AsyncClient, device_id: str, device_name: str
    ) -> None:
        existing = await client.get(f"/rest/config/devices/{device_id}")
        if existing.status_code == 200:
            return
        template = await client.get("/rest/config/defaults/device")
        template.raise_for_status()
        device = template.json()
        device.update({"deviceID": device_id, "name": device_name})
        response = await client.put(f"/rest/config/devices/{device_id}", json=device)
        response.raise_for_status()

    async def _ensure_folder(
        self,
        client: httpx.AsyncClient,
        folder: ScopedSyncFolder,
        *,
        device_id: str,
        server_device_id: str,
    ) -> None:
        existing = await client.get(f"/rest/config/folders/{folder.folder_id}")
        if existing.status_code == 200:
            config = existing.json()
        else:
            template = await client.get("/rest/config/defaults/folder")
            template.raise_for_status()
            config = template.json()
            config.update(
                {
                    "id": folder.folder_id,
                    "path": folder.syncthing_path,
                    "type": "sendreceive",
                }
            )
        config["label"] = folder.label
        config["path"] = folder.syncthing_path
        config["paused"] = False
        shared = {item.get("deviceID") for item in config.get("devices", [])}
        for shared_id in (server_device_id, device_id):
            if shared_id not in shared:
                config.setdefault("devices", []).append({"deviceID": shared_id})
        response = await client.put(
            f"/rest/config/folders/{folder.folder_id}", json=config
        )
        response.raise_for_status()
        ignores = await client.post(
            "/rest/db/ignores",
            params={"folder": folder.folder_id},
            json={"ignore": [".obsidian", ".obsidian/**"]},
        )
        ignores.raise_for_status()
        await self.rescan(scope=None, folder=folder, client=client)

    async def rescan(
        self,
        scope: Optional[MemoryScope],
        *,
        folder: Optional[ScopedSyncFolder] = None,
        client: Optional[httpx.AsyncClient] = None,
        space_name: Optional[str] = None,
    ) -> None:
        target = folder or self.folder(scope, space_name=space_name)  # type: ignore[arg-type]
        if client is not None:
            response = await client.post(
                "/rest/db/scan", params={"folder": target.folder_id}
            )
            response.raise_for_status()
            return
        async with self._client() as owned:
            response = await owned.post(
                "/rest/db/scan", params={"folder": target.folder_id}
            )
            response.raise_for_status()

    async def health(
        self, scope: MemoryScope, *, space_name: Optional[str] = None
    ) -> dict:
        folder = self.folder(scope, space_name=space_name)
        async with self._client() as client:
            config_response = await client.get(
                f"/rest/config/folders/{folder.folder_id}"
            )
            if config_response.status_code == 404:
                return {
                    "configured": False,
                    "healthy": False,
                    "folder_id": folder.folder_id,
                    "state": "unpaired",
                    "devices": [],
                    "warnings": [],
                }
            config_response.raise_for_status()
            config = config_response.json()
            status_response = await client.get(
                "/rest/db/status", params={"folder": folder.folder_id}
            )
            status_response.raise_for_status()
            status = status_response.json()
            connections_response = await client.get("/rest/system/connections")
            connections_response.raise_for_status()
            connections = connections_response.json().get("connections", {})
            server_id = await self._server_device_id(client)
            devices = []
            warnings = []
            for item in config.get("devices", []):
                device_id = item.get("deviceID")
                if not device_id or device_id == server_id:
                    continue
                connected = bool(connections.get(device_id, {}).get("connected"))
                devices.append({"device_id": device_id, "connected": connected})
                if not connected:
                    warnings.append(f"Device {device_id[:7]} is offline")
            error = status.get("error") or None
            state = status.get("state")
            healthy = not error and state in {"idle", "scanning", "scan-waiting"}
            return {
                "configured": True,
                "healthy": healthy,
                "folder_id": folder.folder_id,
                "state": state,
                "error": error,
                "devices": devices,
                "warnings": warnings,
                "paused": bool(config.get("paused")),
            }

    async def set_frozen(
        self,
        scope: MemoryScope,
        frozen: bool,
        *,
        space_name: Optional[str] = None,
    ) -> dict:
        folder = self.folder(scope, space_name=space_name)
        async with self._client() as client:
            existing = await client.get(f"/rest/config/folders/{folder.folder_id}")
            if existing.status_code == 404:
                raise MemoryScopeError("Vault sync folder is not paired")
            existing.raise_for_status()
            if frozen:
                await self.rescan(scope=None, folder=folder, client=client)
            config = existing.json()
            config["paused"] = frozen
            response = await client.put(
                f"/rest/config/folders/{folder.folder_id}", json=config
            )
            response.raise_for_status()
            if not frozen:
                await self.rescan(scope=None, folder=folder, client=client)
        return await self.health(scope, space_name=space_name)


vault_sync_broker = VaultSyncBroker()
