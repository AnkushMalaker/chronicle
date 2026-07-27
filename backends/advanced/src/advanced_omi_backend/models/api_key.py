"""Long-lived API keys for authenticating non-browser clients.

A Chronicle JWT expires after ``JWT_LIFETIME_SECONDS`` (24h), which is fine for
the WebUI but useless for a client that stores one credential and never sees a
login form again — dictation apps (Handy), relays, sync daemons. Those clients
either break daily or, worse, end up storing the account password so they can
re-login on every reconnect.

An API key is a bearer credential that authenticates as its owning user with
the same access a JWT would grant, but does not expire unless given an explicit
``expires_at`` or revoked. It is presented exactly like a JWT
(``Authorization: Bearer <token>``), so clients that only offer an "API key"
field need no changes.

Only the SHA-256 of the secret is stored. The plaintext token is returned once
at creation and is unrecoverable afterwards.
"""

import hashlib
import logging
import secrets
from datetime import UTC, datetime
from typing import Optional

import pymongo
from beanie import Document, PydanticObjectId
from pydantic import Field

logger = logging.getLogger(__name__)

# Token layout: chrn_<prefix>_<secret>
#   prefix — public, indexed, identifies the row without revealing the secret
#   secret — only ever stored as a SHA-256 digest
TOKEN_SCHEME = "chrn"
PREFIX_BYTES = 6
SECRET_BYTES = 32

# last_used_at is written on every authenticated request; a per-request DB write
# would tax hot paths for no benefit, so it is only refreshed once per interval.
LAST_USED_REFRESH_SECONDS = 300


def hash_secret(secret: str) -> str:
    """SHA-256 of an API key secret.

    Plain SHA-256 rather than a password hash: the secret is 32 random bytes,
    so there is nothing to brute-force, and this runs on every request.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Mint a new token. Returns (full_token, prefix, secret_hash)."""
    prefix = secrets.token_hex(PREFIX_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return f"{TOKEN_SCHEME}_{prefix}_{secret}", prefix, hash_secret(secret)


def parse_token(token: str) -> Optional[tuple[str, str]]:
    """Split a bearer credential into (prefix, secret), or None if it is not an
    API key. A JWT never matches, so this doubles as the discriminator between
    the two credential types."""
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_SCHEME:
        return None
    prefix, secret = parts[1], parts[2]
    if not prefix or not secret:
        return None
    return prefix, secret


class ApiKey(Document):
    """A long-lived bearer credential belonging to a user."""

    user_id: PydanticObjectId
    # Human-readable label so a key can be recognized (and revoked) later.
    name: str
    key_prefix: str
    key_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_used_at: Optional[datetime] = None
    # None = never expires. This is the point of the feature; an expiry is opt-in.
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None

    class Settings:
        name = "api_keys"
        indexes = [
            pymongo.IndexModel([("key_prefix", pymongo.ASCENDING)], unique=True),
            pymongo.IndexModel([("user_id", pymongo.ASCENDING)]),
        ]

    def is_usable(self) -> bool:
        """Whether this key may still authenticate a request."""
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and datetime.now(UTC) >= _as_utc(
            self.expires_at
        ):
            return False
        return True


def _as_utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; treat those as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def resolve_api_key(token: str) -> Optional[ApiKey]:
    """Look up and verify a bearer credential as an API key.

    Returns the ApiKey if the token is well-formed, matches a stored key, and
    that key is neither revoked nor expired. Returns None otherwise — including
    for tokens that are not API keys at all, so callers can fall through to JWT
    verification.
    """
    parsed = parse_token(token)
    if parsed is None:
        return None
    prefix, secret = parsed

    api_key = await ApiKey.find_one({"key_prefix": prefix})
    if api_key is None:
        return None
    if not secrets.compare_digest(api_key.key_hash, hash_secret(secret)):
        logger.warning(f"API key prefix {prefix} presented with a bad secret")
        return None
    if not api_key.is_usable():
        return None
    return api_key


async def touch_api_key(api_key: ApiKey) -> None:
    """Refresh last_used_at, at most once per LAST_USED_REFRESH_SECONDS.

    Failures are swallowed: usage tracking must never turn a valid request into
    a 401.
    """
    now = datetime.now(UTC)
    if (
        api_key.last_used_at is not None
        and (now - _as_utc(api_key.last_used_at)).total_seconds()
        < LAST_USED_REFRESH_SECONDS
    ):
        return
    try:
        await api_key.set({"last_used_at": now})
    except Exception as e:
        logger.debug(f"Could not update last_used_at for key {api_key.key_prefix}: {e}")
