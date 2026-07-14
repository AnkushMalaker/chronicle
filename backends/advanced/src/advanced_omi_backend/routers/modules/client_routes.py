"""
Client management routes for Chronicle API.

Handles active client monitoring and management.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.client_manager import (
    ClientManager,
    get_client_manager_dependency,
)
from advanced_omi_backend.controllers import client_controller
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clients", tags=["clients"])


class RenameDeviceRequest(BaseModel):
    name: str


@router.get("/active")
async def get_active_clients(
    current_user: User = Depends(current_active_user),
    client_manager: ClientManager = Depends(get_client_manager_dependency),
):
    """Get information about active clients. Users see only their own clients, admins see all."""
    return await client_controller.get_active_clients(current_user, client_manager)


@router.get("")
async def list_devices(
    current_user: User = Depends(current_active_user),
    client_manager: ClientManager = Depends(get_client_manager_dependency),
):
    """List remembered devices with live status. Admins see all users' devices."""
    return await client_controller.list_devices(current_user, client_manager)


@router.patch("/{client_id}")
async def rename_device(
    client_id: str,
    body: RenameDeviceRequest,
    current_user: User = Depends(current_active_user),
):
    """Set a device's friendly display name (does not change its stable client_id)."""
    return await client_controller.rename_device(current_user, client_id, body.name)


@router.delete("/{client_id}")
async def forget_device(
    client_id: str,
    current_user: User = Depends(current_active_user),
    client_manager: ClientManager = Depends(get_client_manager_dependency),
):
    """Forget a remembered device (also evicts it if currently connected)."""
    return await client_controller.forget_device(
        current_user, client_id, client_manager
    )
