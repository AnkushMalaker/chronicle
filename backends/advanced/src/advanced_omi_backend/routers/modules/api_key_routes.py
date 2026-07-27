"""API key management.

Lets a user mint long-lived bearer credentials for clients that cannot re-login
(dictation apps, relays, sync daemons). Keys authenticate as the owning user
with the same access their JWT would grant.

The plaintext token is returned exactly once, by POST /api/api-keys. Listing a
key only ever shows its public prefix.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.api_key import ApiKey, generate_token
from advanced_omi_backend.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # Omit for a key that never expires — the usual case for a device client.
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ApiKeyRead(BaseModel):
    id: str
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]

    @classmethod
    def from_doc(cls, doc: ApiKey) -> "ApiKeyRead":
        return cls(
            id=str(doc.id),
            name=doc.name,
            key_prefix=doc.key_prefix,
            created_at=doc.created_at,
            last_used_at=doc.last_used_at,
            expires_at=doc.expires_at,
        )


class ApiKeyCreated(ApiKeyRead):
    """Creation response — the only time the plaintext token is ever returned."""

    token: str


@router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(current_user: User = Depends(current_active_user)):
    """List the current user's active (non-revoked) API keys."""
    keys = await ApiKey.find({"user_id": current_user.id, "revoked_at": None}).to_list()
    keys.sort(key=lambda k: k.created_at, reverse=True)
    return [ApiKeyRead.from_doc(k) for k in keys]


@router.post("", response_model=ApiKeyCreated, status_code=201)
async def create_api_key(
    payload: ApiKeyCreate, current_user: User = Depends(current_active_user)
):
    """Mint a new API key. The returned `token` is shown once and not stored."""
    token, prefix, key_hash = generate_token()
    expires_at = (
        datetime.now(UTC) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )

    api_key = ApiKey(
        user_id=current_user.id,
        name=payload.name.strip(),
        key_prefix=prefix,
        key_hash=key_hash,
        expires_at=expires_at,
    )
    await api_key.insert()
    logger.info(
        f"User {current_user.email} created API key '{api_key.name}' ({prefix}), "
        f"expires: {expires_at or 'never'}"
    )

    return ApiKeyCreated(**ApiKeyRead.from_doc(api_key).model_dump(), token=token)


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str, current_user: User = Depends(current_active_user)
):
    """Revoke a key. Revocation is a tombstone, not a delete, so the audit trail
    of which prefix was in use survives."""
    try:
        object_id = PydanticObjectId(key_id)
    except Exception:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key = await ApiKey.get(object_id)
    # Ownership is checked here rather than via a scoped query so a superuser
    # still cannot silently revoke another user's key by guessing an id.
    if api_key is None or api_key.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.revoked_at is None:
        await api_key.set({"revoked_at": datetime.now(UTC)})
        logger.info(
            f"User {current_user.email} revoked API key '{api_key.name}' "
            f"({api_key.key_prefix})"
        )
    return None
