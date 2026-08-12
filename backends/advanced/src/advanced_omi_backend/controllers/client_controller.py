"""
Client controller for handling client-related business logic.
"""

import logging
import time

from fastapi.responses import JSONResponse

from advanced_omi_backend.client_manager import (
    ClientManager,
    get_client_manager,
    get_user_clients_active,
)
from advanced_omi_backend.config import WS_IDLE_TIMEOUT_SECS
from advanced_omi_backend.users import (
    RegisteredClient,
    User,
    forget_client_for_user,
    get_user_by_client_id,
    rename_client_for_user,
)

logger = logging.getLogger(__name__)


def _device_view(
    entry: RegisteredClient, client_manager: ClientManager, now: float
) -> dict:
    """Shape one registry device joined with its live connection state.

    `connected` and `last_seen` come from the in-memory ClientState when the device is
    live (authoritative), falling back to the persisted registry timestamp when offline.
    """
    client_id = entry.client_id
    state = client_manager.get_client(client_id)
    if state is not None:
        last_seen = max(0.0, now - state.last_activity)
        connected = last_seen < WS_IDLE_TIMEOUT_SECS
        has_active = bool(state.stream_session_id) or state.batch_started
    else:
        last_seen = max(0.0, now - entry.last_seen.timestamp())
        connected = False
        has_active = False
    return {
        "client_id": client_id,
        "device_name": entry.device_name,
        "name": entry.name or entry.device_name or client_id,
        "connected": connected,
        "last_seen": round(last_seen, 1),
        "has_active_conversation": has_active,
    }


async def list_devices(user: User, client_manager: ClientManager) -> dict:
    """List remembered devices joined with live status. Admins see every user's devices;
    regular users see only their own."""
    now = time.time()
    if user.is_superuser:
        users = await User.find_all().to_list()
    else:
        users = [user]
    devices = []
    for u in users:
        owner_email = u.email
        for entry in u.registered_clients:
            view = _device_view(entry, client_manager, now)
            view["user_email"] = owner_email
            devices.append(view)
    devices.sort(key=lambda d: (not d["connected"], d["last_seen"] or 1e18))
    return {"devices": devices, "total_count": len(devices)}


async def rename_device(user: User, client_id: str, name: str):
    """Set a device's friendly display name (caller's own device, or any for admins)."""
    name = (name or "").strip()
    if not name:
        return JSONResponse(
            status_code=400, content={"error": "name must not be empty"}
        )

    owner = user if user.has_client(client_id) else None
    if owner is None and user.is_superuser:
        owner = await get_user_by_client_id(client_id)
    if owner is None:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    if await rename_client_for_user(owner, client_id, name):
        return {"client_id": client_id, "name": name}
    return JSONResponse(status_code=404, content={"error": "Device not found"})


async def forget_device(user: User, client_id: str, client_manager: ClientManager):
    """Remove a device from the registry. A currently-connected device is also evicted
    so it doesn't immediately re-appear from its live ClientState."""
    owner = user if user.has_client(client_id) else None
    if owner is None and user.is_superuser:
        owner = await get_user_by_client_id(client_id)
    if owner is None:
        return JSONResponse(status_code=404, content={"error": "Device not found"})

    if client_manager.has_client(client_id):
        await client_manager.remove_client_with_cleanup(client_id)

    if await forget_client_for_user(owner, client_id):
        return {"client_id": client_id, "forgotten": True}
    return JSONResponse(status_code=404, content={"error": "Device not found"})


async def get_active_clients(user: User, client_manager: ClientManager):
    """Get information about active clients. Users see only their own clients, admins see all."""
    try:
        if not client_manager.is_initialized():
            return JSONResponse(
                status_code=503,
                content={"error": "Client manager not available"},
            )

        if user.is_superuser:
            # Admin: return all active clients
            return {
                "active_clients": client_manager.get_client_info_summary(),
                "total_count": client_manager.get_client_count(),
            }
        else:
            # Regular user: return only their own clients
            user_active_clients = get_user_clients_active(user.user_id)
            all_clients = client_manager.get_client_info_summary()

            # Filter to only the user's clients
            user_clients = [
                client
                for client in all_clients
                if client["client_id"] in user_active_clients
            ]

            return {
                "active_clients": user_clients,
                "total_count": len(user_clients),
            }

    except Exception as e:
        logger.error(f"Error getting active clients: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to get active clients"},
        )
