"""Existence check behind annotation orphan detection.

``_existing_conversation_ids`` decides which annotations are orphans, and the cleanup
endpoint **deletes** what it does not vouch for. So the failure that matters is
under-reporting: an id it fails to return is training data destroyed.

It used to answer by loading the conversations as Beanie models. That is correct but
ruinous — 31 ids pulled 71.7 MB into roughly 354,000 Word and SpeakerSegment instances,
enough allocation to trigger a generation-2 collection and stall the event loop for
seconds. It now asks Mongo for the ids alone, which is what these tests pin down.
"""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.conversation import Conversation
from backend.routers.modules.finetuning_routes import _existing_conversation_ids

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("mongo_service"),
]


@pytest_asyncio.fixture
async def conversations():
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["chronicle_orphan_test"]
    await init_beanie(database=database, document_models=[Conversation])
    await Conversation.find_all().delete()
    yield
    await client.drop_database("chronicle_orphan_test")
    client.close()


async def _insert(cid: str, **kwargs) -> None:
    await Conversation(
        conversation_id=cid,
        audio_uuid=f"audio-{cid}",
        user_id="orphan-test-user",
        client_id="orphan-test-user-dev",
        **kwargs,
    ).insert()


async def test_it_returns_exactly_the_ids_that_exist(conversations):
    await _insert("alive-1")
    await _insert("alive-2")

    found = await _existing_conversation_ids({"alive-1", "alive-2", "gone"})

    assert found == {"alive-1", "alive-2"}


async def test_a_conversation_with_a_transcript_is_still_reported(conversations):
    """The whole point is not loading transcripts — they must not change the answer."""
    version = Conversation.TranscriptVersion(
        version_id="v1",
        transcript="a long conversation " * 500,
        created_at=datetime.now(timezone.utc),
    )
    await _insert(
        "verbose", active_transcript_version="v1", transcript_versions=[version]
    )

    assert await _existing_conversation_ids({"verbose"}) == {"verbose"}


async def test_a_soft_deleted_conversation_still_counts_as_existing(conversations):
    """Orphan means the row is gone, not that it was deleted — deleting the
    annotations of a recoverable conversation would destroy training data."""
    await _insert("soft-deleted", deleted=True)

    assert await _existing_conversation_ids({"soft-deleted"}) == {"soft-deleted"}


async def test_no_ids_asks_nothing_and_orphans_nothing(conversations):
    """An empty input must not degenerate into an unfiltered query."""
    await _insert("alive-1")

    assert await _existing_conversation_ids(set()) == set()


async def test_ids_that_exist_nowhere_are_reported_missing(conversations):
    await _insert("alive-1")

    assert await _existing_conversation_ids({"ghost-a", "ghost-b"}) == set()
