"""
User management routes for Chronicle API.

Handles user CRUD operations and admin user management.
"""

import logging

from fastapi import APIRouter, Depends

from advanced_omi_backend.auth import current_superuser
from advanced_omi_backend.controllers import user_controller
from advanced_omi_backend.users import User, UserCreate, UserUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[User])
async def get_users(current_user: User = Depends(current_superuser)):
    """Get all users. Admin only."""
    return await user_controller.get_users()


@router.post("")
async def create_user(
    user_data: UserCreate, current_user: User = Depends(current_superuser)
):
    """Create a new user. Admin only."""
    return await user_controller.create_user(user_data)


@router.put("/{user_id}")
async def update_user(
    user_id: str, user_data: UserUpdate, current_user: User = Depends(current_superuser)
):
    """Update a user. Admin only."""
    return await user_controller.update_user(user_id, user_data)


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    current_user: User = Depends(current_superuser),
    delete_conversations: bool = False,
    delete_memories: bool = False,
):
    """Delete a user and optionally their associated data. Admin only."""
    return await user_controller.delete_user(
        user_id, delete_conversations, delete_memories
    )
