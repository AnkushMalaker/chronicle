"""User management endpoints."""

from fastapi import APIRouter
from simple_speaker_recognition.core.models import UserRequest, UserResponse
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.queries import UserQueries

router = APIRouter()


@router.get("/users")
async def list_users():
    """List all users."""
    db = get_db_session()
    try:
        users = UserQueries.get_all_users(db)
        return [
            UserResponse(
                id=user.id,
                username=user.username,
                created_at=user.created_at.isoformat(),
            )
            for user in users
        ]
    finally:
        db.close()


@router.post("/users")
async def create_user(request: UserRequest):
    """Create or get existing user."""
    db = get_db_session()
    try:
        user = UserQueries.get_or_create_user(db, request.username)
        return UserResponse(
            id=user.id, username=user.username, created_at=user.created_at.isoformat()
        )
    finally:
        db.close()
