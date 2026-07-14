"""
Backward compatibility module - re-exports User models from models.user.

This module maintains the original import location for existing code.
New code should import from advanced_omi_backend.models.user instead.
"""

from advanced_omi_backend.models.user import (
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
