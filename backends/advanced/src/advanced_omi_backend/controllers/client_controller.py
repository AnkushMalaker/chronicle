"""
Client controller for handling client-related business logic.
"""

import logging

from fastapi.responses import JSONResponse

from advanced_omi_backend.client_manager import ClientManager, get_user_clients_active
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)


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
