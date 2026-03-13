"""
User controller for handling user-related business logic.
"""

import asyncio
import logging

from bson import ObjectId
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from advanced_omi_backend.auth import ADMIN_EMAIL, UserManager, get_user_db
from advanced_omi_backend.client_manager import get_user_clients_all
from advanced_omi_backend.database import db, users_col
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.users import User, UserCreate, UserUpdate

logger = logging.getLogger(__name__)


async def get_users():
    """Get all users."""
    try:
        users = []
        async for user_doc in users_col.find():
            user = User(**user_doc)
            users.append(user)
        return users
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        raise HTTPException(status_code=500, detail="Error fetching users")


async def create_user(user_data: UserCreate):
    """Create a new user."""
    try:
        # Get user database and create user manager
        user_db_gen = get_user_db()
        user_db = await anext(user_db_gen)
        user_manager = UserManager(user_db)

        # Check if user already exists
        try:
            existing_user = await user_manager.get_by_email(user_data.email)
            # If we get here, user exists
            return JSONResponse(
                status_code=409,
                content={
                    "message": f"User with email {user_data.email} already exists"
                },
            )
        except Exception:
            # User doesn't exist, continue with creation
            pass

        # Create the user through the user manager
        user = await user_manager.create(user_data)

        # Return the full user object (serialized via UserRead schema)
        from advanced_omi_backend.models.user import UserRead

        user_read = UserRead.model_validate(user)

        return JSONResponse(
            status_code=201,
            content=user_read.model_dump(mode="json"),
        )

    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error creating user: {e}")
        logger.error(f"Full traceback: {error_details}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error creating user: {str(e)}"},
        )


async def update_user(user_id: str, user_data: UserUpdate):
    """Update an existing user."""
    try:
        # Validate ObjectId format
        try:
            object_id = ObjectId(user_id)
        except Exception as e:
            logger.error(f"Invalid ObjectId format for user_id {user_id}: {e}")
            return JSONResponse(
                status_code=400,
                content={
                    "message": f"Invalid user_id format: {user_id}. Must be a valid ObjectId."
                },
            )

        # Check if user exists
        existing_user = await users_col.find_one({"_id": object_id})
        if not existing_user:
            return JSONResponse(
                status_code=404, content={"message": f"User {user_id} not found"}
            )

        # Get user database and create user manager
        user_db_gen = get_user_db()
        user_db = await anext(user_db_gen)
        user_manager = UserManager(user_db)

        # Convert to User object for the manager
        user_obj = User(**existing_user)

        # Update the user using the fastapi-users manager
        # Note: signature is update(user_update, user) - update data first, then user object
        updated_user = await user_manager.update(user_data, user_obj)

        # Return the full user object (serialized via UserRead schema)
        from advanced_omi_backend.models.user import UserRead

        user_read = UserRead.model_validate(updated_user)

        return JSONResponse(
            status_code=200,
            content=user_read.model_dump(mode="json"),
        )

    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error updating user: {e}")
        logger.error(f"Full traceback: {error_details}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error updating user: {str(e)}"},
        )


async def delete_user(
    user_id: str,
    delete_conversations: bool = False,
    delete_memories: bool = False,
):
    """Delete a user and optionally their associated data."""
    try:
        # Validate ObjectId format
        try:
            object_id = ObjectId(user_id)
        except Exception as e:
            logging.error(f"Invalid ObjectId format for user_id {user_id}: {e}")
            return JSONResponse(
                status_code=400,
                content={
                    "message": f"Invalid user_id format: {user_id}. Must be a valid ObjectId."
                },
            )

        # Check if user exists
        existing_user = await users_col.find_one({"_id": object_id})
        if not existing_user:
            return JSONResponse(
                status_code=404, content={"message": f"User {user_id} not found"}
            )

        # Prevent deletion of administrator user
        user_email = existing_user.get("email", "")
        is_superuser = existing_user.get("is_superuser", False)

        if is_superuser or user_email == ADMIN_EMAIL:
            return JSONResponse(
                status_code=403,
                content={
                    "message": f"Cannot delete administrator user. Admin users are protected from deletion."
                },
            )

        deleted_data = {}

        # Delete user from users collection
        user_result = await users_col.delete_one({"_id": object_id})
        deleted_data["user_deleted"] = user_result.deleted_count > 0

        if delete_conversations:
            # Delete all conversations for this user
            conversations_result = await Conversation.find(
                Conversation.user_id == user_id
            ).delete()
            deleted_data["conversations_deleted"] = conversations_result.deleted_count

        if delete_memories:
            # Delete all memories for this user using the memory service
            try:
                memory_service = get_memory_service()
                memory_count = await asyncio.get_running_loop().run_in_executor(
                    None, memory_service.delete_all_user_memories, user_id
                )
                deleted_data["memories_deleted"] = memory_count
            except Exception as mem_error:
                logger.error(f"Error deleting memories for user {user_id}: {mem_error}")
                deleted_data["memories_deleted"] = 0
                deleted_data["memory_deletion_error"] = str(mem_error)

        # Build message based on what was deleted
        message = f"User {user_id} deleted successfully"
        deleted_items = []
        if delete_conversations and deleted_data.get("conversations_deleted", 0) > 0:
            deleted_items.append(
                f"{deleted_data['conversations_deleted']} conversations"
            )
        if delete_memories and deleted_data.get("memories_deleted", 0) > 0:
            deleted_items.append(f"{deleted_data['memories_deleted']} memories")

        if deleted_items:
            message += f" along with {', '.join(deleted_items)}"

        return JSONResponse(
            content={
                "message": message,
                "deleted_data": deleted_data,
            }
        )

    except Exception as e:
        logger.error(f"Error deleting user {user_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"message": f"Error deleting user: {str(e)}"},
        )
