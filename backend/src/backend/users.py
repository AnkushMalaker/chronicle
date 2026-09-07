"""
Backward compatibility module - re-exports User models from models.user.

This module maintains the original import location for existing code.
New code should import from backend.models.user instead.
"""

from backend.models.user import (
    RegisteredClient,
    User,
    UserCreate,
    UserRead,
    UserUpdate,
    forget_client_for_user,
    get_user_by_client_id,
    get_user_by_id,
    get_user_db,
    register_client_to_user,
    rename_client_for_user,
    touch_client_last_seen,
)

__all__ = [
    "RegisteredClient",
    "User",
    "UserCreate",
    "UserRead",
    "UserUpdate",
    "get_user_db",
    "get_user_by_id",
    "get_user_by_client_id",
    "register_client_to_user",
    "rename_client_for_user",
    "forget_client_for_user",
    "touch_client_last_seen",
]
