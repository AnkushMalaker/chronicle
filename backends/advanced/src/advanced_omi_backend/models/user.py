"""User models for fastapi-users integration with Beanie and MongoDB."""

import logging
from datetime import UTC, datetime
from typing import Optional

from beanie import Document, PydanticObjectId
from fastapi_users.db import BeanieBaseUser, BeanieUserDatabase
from fastapi_users.schemas import BaseUser, BaseUserCreate, BaseUserUpdate
from pydantic import ConfigDict, EmailStr, Field

logger = logging.getLogger(__name__)


class UserCreate(BaseUserCreate):
    """Schema for creating new users."""

    display_name: Optional[str] = None
    assistant_name: Optional[str] = None
    notification_email: Optional[EmailStr] = None
    is_superuser: Optional[bool] = False


class UserRead(BaseUser[PydanticObjectId]):
    """Schema for reading user data."""

    display_name: Optional[str] = None
    assistant_name: Optional[str] = None
    notification_email: Optional[EmailStr] = None
    registered_clients: dict[str, dict] = Field(default_factory=dict)
    primary_speakers: list[dict] = Field(default_factory=list)


class UserUpdate(BaseUserUpdate):
    """Schema for updating user data."""

    display_name: Optional[str] = None
    assistant_name: Optional[str] = None
    notification_email: Optional[EmailStr] = None
    is_superuser: Optional[bool] = None

    def create_update_dict(self):
        """Create update dictionary for regular user operations."""
        update_dict = super().create_update_dict()
        if self.display_name is not None:
            update_dict["display_name"] = self.display_name
        if self.assistant_name is not None:
            update_dict["assistant_name"] = self.assistant_name
        if self.notification_email is not None:
            update_dict["notification_email"] = self.notification_email
        return update_dict

    def create_update_dict_superuser(self):
        """Create update dictionary for superuser operations."""
        update_dict = super().create_update_dict_superuser()
        if self.display_name is not None:
            update_dict["display_name"] = self.display_name
        if self.assistant_name is not None:
            update_dict["assistant_name"] = self.assistant_name
        if self.notification_email is not None:
            update_dict["notification_email"] = self.notification_email
        return update_dict


class User(BeanieBaseUser, Document):
    """User model extending fastapi-users BeanieBaseUser with custom fields."""

    # Pydantic v2 configuration
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
    )

    display_name: Optional[str] = None
    # Name used to label the assistant's turns when extracting memories from chat
    assistant_name: Optional[str] = None
    notification_email: Optional[EmailStr] = None
    # Client tracking for audio devices
    registered_clients: dict[str, dict] = Field(default_factory=dict)
    # Speaker processing filter configuration
    primary_speakers: list[dict] = Field(default_factory=list)

    class Settings:
        name = "users"  # Collection name in MongoDB - standardized from "fastapi_users"
        email_collation = {"locale": "en", "strength": 2}  # Case-insensitive comparison

    @property
    def user_id(self) -> str:
        """Return string representation of MongoDB ObjectId for backward compatibility."""
        return str(self.id)

    def register_client(
        self, client_id: str, device_name: Optional[str] = None
    ) -> None:
        """Register (auto-add) or refresh a device for this user.

        Devices are remembered across disconnects/restarts. The friendly ``name`` is
        user-editable (defaults to the device name) and is NEVER overwritten here on a
        reconnect — only ``last_seen`` and the technical ``device_name`` are refreshed.
        Liveness is not stored: whether a device is connected right now is derived from
        the in-memory ClientState, not a persisted flag.
        """
        existing = self.registered_clients.get(client_id)
        if existing:
            existing["last_seen"] = datetime.now(UTC)
            if device_name:
                existing["device_name"] = device_name
            # Backfill a friendly name for devices registered before naming existed.
            existing.setdefault("name", device_name or client_id)
            return

        self.registered_clients[client_id] = {
            "client_id": client_id,
            "device_name": device_name,
            "name": device_name or client_id,  # editable display label
            "first_seen": datetime.now(UTC),
            "last_seen": datetime.now(UTC),
        }

    def touch_client(self, client_id: str) -> bool:
        """Stamp a device's last_seen (e.g. on disconnect). Returns False if unknown."""
        device = self.registered_clients.get(client_id)
        if device is None:
            return False
        device["last_seen"] = datetime.now(UTC)
        return True

    def set_client_name(self, client_id: str, name: str) -> bool:
        """Set a device's friendly display name. Returns False if unknown."""
        device = self.registered_clients.get(client_id)
        if device is None:
            return False
        device["name"] = name
        return True

    def forget_client(self, client_id: str) -> bool:
        """Remove a device from the registry. Returns False if unknown."""
        return self.registered_clients.pop(client_id, None) is not None

    def get_client_ids(self) -> list[str]:
        """Get all client IDs registered to this user."""
        return list(self.registered_clients.keys())


# Rebuild Pydantic model to ensure inherited fields are properly accessible
User.model_rebuild()


async def get_user_db():
    """Get the user database instance for dependency injection."""
    yield BeanieUserDatabase(User)  # type: ignore


async def get_user_by_id(user_id: str) -> Optional[User]:
    """Get user by MongoDB ObjectId string."""
    try:
        return await User.get(PydanticObjectId(user_id))
    except Exception as e:
        logger.error(f"Failed to get user by ID {user_id}: {e}")
        # Re-raise for proper error handling upstream
        raise


async def get_user_by_client_id(client_id: str) -> Optional[User]:
    """Find the user that owns a specific client_id."""
    return await User.find_one({"registered_clients.client_id": client_id})


async def register_client_to_user(
    user: User, client_id: str, device_name: Optional[str] = None
) -> None:
    """Register a client to a user and save to database."""
    user.register_client(client_id, device_name)
    await user.save()


async def touch_client_last_seen(client_id: str) -> None:
    """Stamp a device's last_seen in the registry (e.g. on disconnect). No-op if the
    client_id isn't owned by any user."""
    user = await get_user_by_client_id(client_id)
    if user and user.touch_client(client_id):
        await user.save()


async def rename_client_for_user(user: User, client_id: str, name: str) -> bool:
    """Set a device's friendly name and persist. Returns False if unknown."""
    if user.set_client_name(client_id, name):
        await user.save()
        return True
    return False


async def forget_client_for_user(user: User, client_id: str) -> bool:
    """Remove a device from the registry and persist. Returns False if unknown."""
    if user.forget_client(client_id):
        await user.save()
        return True
    return False
