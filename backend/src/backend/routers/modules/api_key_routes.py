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

from backend.auth import current_active_user
from backend.models.api_key import ApiKey, generate_token
from backend.models.user import User, get_user_by_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # Omit for a key that never expires — the usual case for a device client.
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ApiKeyRead(BaseModel):
    id: str
    user_id: str
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]

    @classmethod
    def from_doc(cls, doc: ApiKey) -> "ApiKeyRead":
        return cls(
            id=str(doc.id),
            user_id=str(doc.user_id),
            name=doc.name,
            key_prefix=doc.key_prefix,
            created_at=doc.created_at,
            last_used_at=doc.last_used_at,
            expires_at=doc.expires_at,
        )


def _resolve_owner(
    requested_user_id: Optional[str], current_user: User
) -> PydanticObjectId:
    """Whose keys this request may act on.

    Managing another user's keys is a superuser action: a key grants that
    user's full access, so minting one for someone else is equivalent to
    impersonating them. Regular users are silently scoped to themselves rather
    than erroring, so the common case needs no parameter at all.
    """
    if requested_user_id is None:
        return current_user.id
    try:
        owner_id = PydanticObjectId(requested_user_id)
    except Exception:
        raise HTTPException(status_code=404, detail="User not found")
    if owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="Only admins can manage another user's API keys"
        )
    return owner_id


class ApiKeyCreated(ApiKeyRead):
    """Creation response — the only time the plaintext token is ever returned."""

    token: str


@router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(
    user_id: Optional[str] = None, current_user: User = Depends(current_active_user)
):
    """List active (non-revoked) API keys. Defaults to the caller's own; admins
    may pass `user_id` to inspect another user's."""
    owner_id = _resolve_owner(user_id, current_user)
    keys = await ApiKey.find({"user_id": owner_id, "revoked_at": None}).to_list()
    keys.sort(key=lambda k: k.created_at, reverse=True)
    return [ApiKeyRead.from_doc(k) for k in keys]


@router.post("", response_model=ApiKeyCreated, status_code=201)
async def create_api_key(
    payload: ApiKeyCreate,
    user_id: Optional[str] = None,
    current_user: User = Depends(current_active_user),
):
    """Mint a new API key. The returned `token` is shown once and not stored.

    Defaults to the caller's own account; admins may pass `user_id` to mint a
    key on another user's behalf (e.g. provisioning a device for them).
    """
    owner_id = _resolve_owner(user_id, current_user)
    if owner_id != current_user.id and await get_user_by_id(str(owner_id)) is None:
        raise HTTPException(status_code=404, detail="User not found")

    token, prefix, key_hash = generate_token()
    expires_at = (
        datetime.now(UTC) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )

    api_key = ApiKey(
        user_id=owner_id,
        name=payload.name.strip(),
        key_prefix=prefix,
        key_hash=key_hash,
        expires_at=expires_at,
    )
    await api_key.insert()
    logger.info(
        f"User {current_user.email} created API key '{api_key.name}' ({prefix}) "
        f"for user {owner_id}, expires: {expires_at or 'never'}"
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
    # Ownership is enforced here rather than by a scoped query so a non-admin
    # cannot revoke someone else's key by guessing an id.
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.revoked_at is None:
        await api_key.set({"revoked_at": datetime.now(UTC)})
        logger.info(
            f"User {current_user.email} revoked API key '{api_key.name}' "
            f"({api_key.key_prefix})"
        )
    return None
