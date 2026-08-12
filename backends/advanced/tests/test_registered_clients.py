"""A device registration has to be findable from the device's own id.

``registered_clients`` used to be a ``{client_id: {...}}`` mapping while the inverse
lookup queried ``registered_clients.client_id``. MongoDB matches a dotted path across
the elements of an *array*, not across arbitrary object keys, so that query matched
nothing for every real device — silently, because the callers treat "no owner" as an
ordinary outcome:

- ``touch_client_last_seen`` no-ops, so a disconnect never stamps ``last_seen``;
- the admin fallback in ``rename_device``/``forget_device`` 404s, so a superuser
  cannot act on another user's device.

Verified against real MongoDB:

    MONGODB_URI=mongodb://localhost:27018 uv run pytest tests/test_registered_clients.py
"""

import os

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.user import (
    RegisteredClient,
    User,
    get_user_by_client_id,
    touch_client_last_seen,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("mongo_service"),
]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def init_db():
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    name = os.getenv("TEST_DB_NAME", "test_registered_clients")
    await init_beanie(database=client[name], document_models=[User])
    yield
    await client.drop_database(name)
    client.close()


@pytest_asyncio.fixture(loop_scope="session")
async def clean(init_db):
    await User.delete_all()
    yield


async def _make_user(email: str, *, devices: list[str]) -> User:
    user = User(
        email=email,
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    await user.insert()
    for device in devices:
        user.register_client(device, device.split("-", 1)[-1])
    await user.save()
    return user


async def test_a_registered_device_is_findable_by_its_client_id(clean):
    user = await _make_user("owner@example.com", devices=["a421c9-havpe"])

    found = await get_user_by_client_id("a421c9-havpe")

    assert found is not None
    assert found.id == user.id


async def test_an_unregistered_client_id_has_no_owner(clean):
    await _make_user("owner@example.com", devices=["a421c9-havpe"])

    assert await get_user_by_client_id("a421c9-omi") is None


async def test_lookup_does_not_cross_user_ownership(clean):
    await _make_user("one@example.com", devices=["a421c9-phone"])
    two = await _make_user("two@example.com", devices=["b53d70-phone"])

    found = await get_user_by_client_id("b53d70-phone")

    assert found is not None
    assert found.id == two.id
    assert found.email == "two@example.com"


async def test_disconnect_stamps_last_seen_on_the_owning_user(clean):
    """The regression that hid the bug: this path swallows a missing owner."""

    user = await _make_user("owner@example.com", devices=["a421c9-havpe"])
    before = user.find_client("a421c9-havpe").last_seen

    await touch_client_last_seen("a421c9-havpe")

    reloaded = await User.get(user.id)
    assert reloaded.find_client("a421c9-havpe").last_seen > before


async def test_registrations_round_trip_through_mongo_as_typed_clients(clean):
    user = await _make_user("owner@example.com", devices=["a421c9-havpe"])

    reloaded = await User.get(user.id)

    assert [type(entry) for entry in reloaded.registered_clients] == [RegisteredClient]
    assert reloaded.get_client_ids() == ["a421c9-havpe"]
    assert reloaded.has_client("a421c9-havpe") is True
    assert reloaded.has_client("a421c9-omi") is False


async def test_forgetting_one_device_leaves_the_others(clean):
    user = await _make_user("owner@example.com", devices=["a421c9-havpe", "a421c9-omi"])

    assert user.forget_client("a421c9-havpe") is True
    assert user.forget_client("a421c9-havpe") is False
    await user.save()

    assert (await User.get(user.id)).get_client_ids() == ["a421c9-omi"]
    assert await get_user_by_client_id("a421c9-havpe") is None
    assert (await get_user_by_client_id("a421c9-omi")).id == user.id
