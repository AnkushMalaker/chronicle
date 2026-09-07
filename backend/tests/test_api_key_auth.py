"""API key minting, verification, and the JWT fall-through in the auth strategy."""

from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from fastapi import HTTPException

from backend import auth
from backend.models import api_key as api_key_model
from backend.models.api_key import ApiKey, generate_token, hash_secret, parse_token
from backend.routers.modules.api_key_routes import _resolve_owner
from backend.routers.modules.user_routes import UserAdminRead


def test_generated_token_round_trips_to_its_stored_hash():
    token, prefix, key_hash = generate_token()

    parsed = parse_token(token)
    assert parsed is not None
    parsed_prefix, secret = parsed
    assert parsed_prefix == prefix
    assert hash_secret(secret) == key_hash
    # The plaintext secret must not be recoverable from what we persist.
    assert secret not in key_hash
    assert secret not in token.split("_", 2)[1]


def test_generated_tokens_are_unique():
    tokens = {generate_token()[0] for _ in range(50)}
    assert len(tokens) == 50


@pytest.mark.parametrize(
    "token",
    [
        # A JWT — the credential type we must NOT claim.
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",
        "",
        "chrn_",
        "chrn_onlyprefix",
        "chrn__missingprefix",
        "chrn_prefix_",
        "other_prefix_secret",
    ],
)
def test_non_api_key_tokens_are_not_parsed(token):
    assert parse_token(token) is None


def _key(**overrides) -> ApiKey:
    token, prefix, key_hash = generate_token()
    defaults = dict(user_id=None, name="test", key_prefix=prefix, key_hash=key_hash)
    defaults.update(overrides)
    key = ApiKey.model_construct(**defaults)
    key.revoked_at = overrides.get("revoked_at")
    key.expires_at = overrides.get("expires_at")
    return key


def test_key_without_expiry_stays_usable():
    assert _key().is_usable()


def test_revoked_key_is_not_usable():
    assert not _key(revoked_at=datetime.now(UTC)).is_usable()


def test_expired_key_is_not_usable():
    assert not _key(expires_at=datetime.now(UTC) - timedelta(seconds=1)).is_usable()


def test_future_expiry_is_still_usable():
    assert _key(expires_at=datetime.now(UTC) + timedelta(days=1)).is_usable()


def test_naive_expiry_from_mongo_is_treated_as_utc():
    """Mongo returns tz-naive datetimes; comparing them to an aware `now`
    would raise and 500 the request instead of rejecting the key."""
    naive_past = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
    assert not _key(expires_at=naive_past).is_usable()


class _FakeUser:
    def __init__(self, is_active=True):
        self.is_active = is_active
        self.email = "user@example.com"


@pytest.mark.asyncio
async def test_strategy_accepts_a_valid_api_key(monkeypatch):
    resolved = _key(user_id="507f1f77bcf86cd799439011")
    user = _FakeUser()
    touched = []

    monkeypatch.setattr(auth, "resolve_api_key", lambda token: _async(resolved))
    monkeypatch.setattr(auth, "get_user_by_id", lambda uid: _async(user))
    monkeypatch.setattr(auth, "touch_api_key", lambda key: _async(touched.append(key)))

    strategy = auth.get_jwt_strategy()
    assert await strategy.read_token("chrn_abc_secret", None) is user
    assert touched == [resolved]


@pytest.mark.asyncio
async def test_strategy_rejects_a_key_whose_user_is_inactive(monkeypatch):
    monkeypatch.setattr(
        auth,
        "resolve_api_key",
        lambda token: _async(_key(user_id="507f1f77bcf86cd799439011")),
    )
    monkeypatch.setattr(
        auth, "get_user_by_id", lambda uid: _async(_FakeUser(is_active=False))
    )

    strategy = auth.get_jwt_strategy()
    assert await strategy.read_token("chrn_abc_secret", None) is None


@pytest.mark.asyncio
async def test_strategy_falls_through_to_jwt_for_non_api_key_tokens(monkeypatch):
    """A JWT must still authenticate — the WebUI depends on it."""
    seen = []
    monkeypatch.setattr(auth, "resolve_api_key", lambda token: _async(None))

    strategy = auth.get_jwt_strategy()
    sentinel = _FakeUser()

    async def fake_jwt_read(self, token, user_manager):
        seen.append(token)
        return sentinel

    monkeypatch.setattr(auth.JWTStrategy, "read_token", fake_jwt_read)
    assert await strategy.read_token("some.jwt.token", None) is sentinel
    assert seen == ["some.jwt.token"]


@pytest.mark.asyncio
async def test_resolve_rejects_a_key_with_the_wrong_secret(monkeypatch):
    """A guessed secret against a real prefix must not authenticate."""
    _, prefix, key_hash = generate_token()
    stored = ApiKey.model_construct(
        user_id=None, name="t", key_prefix=prefix, key_hash=key_hash
    )
    stored.revoked_at = None
    stored.expires_at = None

    monkeypatch.setattr(
        api_key_model.ApiKey, "find_one", staticmethod(lambda *a, **k: _async(stored))
    )
    assert await api_key_model.resolve_api_key(f"chrn_{prefix}_wrongsecret") is None


async def _async(value):
    return value


class _FakeOwner:
    """Minimal stand-in for User in _resolve_owner authorization checks."""

    def __init__(self, user_id, is_superuser=False):
        self.id = user_id
        self.is_superuser = is_superuser


def test_resolve_owner_defaults_to_the_caller():
    me = _FakeOwner(PydanticObjectId())
    assert _resolve_owner(None, me) == me.id


def test_resolve_owner_lets_a_user_name_themselves():
    me = _FakeOwner(PydanticObjectId())
    assert _resolve_owner(str(me.id), me) == me.id


def test_resolve_owner_blocks_a_non_admin_targeting_someone_else():
    """A key grants its owner's full access, so minting one for another user
    is an impersonation primitive — admins only."""

    me = _FakeOwner(PydanticObjectId())
    victim = PydanticObjectId()
    with pytest.raises(HTTPException) as exc:
        _resolve_owner(str(victim), me)
    assert exc.value.status_code == 403


def test_resolve_owner_allows_an_admin_targeting_someone_else():
    admin = _FakeOwner(PydanticObjectId(), is_superuser=True)
    other = PydanticObjectId()
    assert _resolve_owner(str(other), admin) == other


def test_resolve_owner_rejects_a_malformed_user_id():
    admin = _FakeOwner(PydanticObjectId(), is_superuser=True)
    with pytest.raises(HTTPException) as exc:
        _resolve_owner("not-an-object-id", admin)
    assert exc.value.status_code == 404


def test_admin_user_listing_never_serialises_the_password_hash():
    """/api/users previously returned the Beanie document, shipping
    hashed_password to the browser."""

    fields = set(UserAdminRead.model_fields)
    assert "hashed_password" not in fields
    assert {"email", "is_superuser", "is_active", "device_count"} <= fields


def test_admin_user_listing_keeps_the_underscore_id_alias():
    """The WebUI reads user._id; a bare `id` would silently break every row."""

    row = UserAdminRead(
        id="507f1f77bcf86cd799439011",
        email="a@b.com",
        is_superuser=False,
        is_active=True,
        is_verified=True,
        device_count=2,
    )
    dumped = row.model_dump(by_alias=True)
    assert dumped["_id"] == "507f1f77bcf86cd799439011"
    assert "hashed_password" not in dumped
