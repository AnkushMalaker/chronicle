"""
User management routes for Chronicle API.

Handles user CRUD operations and admin user management.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field

from backend.auth import current_superuser
from backend.controllers import user_controller
from backend.users import User, UserCreate, UserUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


class UserAdminRead(BaseModel):
    """A user as the admin Users page sees them.

    Serialising the Beanie ``User`` document directly would ship
    ``hashed_password`` to the browser, so this lists the fields explicitly.
    The id keeps its ``_id`` alias to match what the WebUI already reads.
    """

    id: str = Field(serialization_alias="_id")
    email: EmailStr
    display_name: Optional[str] = None
    is_superuser: bool
    is_active: bool
    is_verified: bool
    device_count: int

    @classmethod
    def from_doc(cls, user: User) -> "UserAdminRead":
        return cls(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            is_superuser=user.is_superuser,
            is_active=user.is_active,
            is_verified=user.is_verified,
            device_count=len(user.registered_clients),
        )


@router.get("", response_model=list[UserAdminRead], response_model_by_alias=True)
async def get_users(current_user: User = Depends(current_superuser)):
    """Get all users. Admin only."""
    users = await user_controller.get_users()
    return [UserAdminRead.from_doc(u) for u in users]


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
