"""Tests for the conversation processing-status reconciler.

The reconciler decides ``processing_status`` for every non-deleted conversation. It
used to do that by loading each one as a full Beanie model, which meant pulling every
transcript through the event loop to read a handful of small fields — 587 MB across
871 documents on the deployment this was measured on, freezing the loop in 185-503 ms
slices for over a minute at every boot.

It now decides in the query. The aggregation pipeline now decides correctness, so the
tests that matter run it against a real MongoDB and check the *verdicts*, not the
shape of the pipeline: the whole risk is that "does the active transcript version have
text" is answered differently server-side than the model property answered it.

The decision rules themselves live in one place, ``Conversation.derive_status``, and
are covered separately without any I/O.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.conversation import Conversation
from backend.services.status_reconciler import reconcile_conversation_statuses

ACTIVE = Conversation.ConversationStatus.ACTIVE.value
COMPLETED = Conversation.ConversationStatus.COMPLETED.value
FAILED = Conversation.ConversationStatus.FAILED.value


# --------------------------------------------------------------------------- #
# The rules, in one place and with no I/O
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_transcript_means_completed_however_settled():
    """The transcript is the deliverable, so it decides on its own."""
    assert Conversation.derive_status(has_transcript=True, settled=True) == (
        COMPLETED,
        None,
    )
    assert Conversation.derive_status(has_transcript=True, settled=False) == (
        COMPLETED,
        None,
    )


@pytest.mark.unit
def test_no_transcript_is_only_a_failure_once_settled():
    """Before that it is 'not yet', which is why the staleness cutoff exists."""
    assert Conversation.derive_status(has_transcript=False, settled=False) == (
        ACTIVE,
        None,
    )
    assert Conversation.derive_status(has_transcript=False, settled=True) == (
        FAILED,
        "transcription",
    )


# --------------------------------------------------------------------------- #
# The pipeline, against a real MongoDB
# --------------------------------------------------------------------------- #


def _conversation(cid: str, **kwargs) -> Conversation:
    return Conversation(
        conversation_id=cid,
        audio_uuid=f"audio-{cid}",
        user_id="reconciler-test-user",
        client_id="reconciler-test-user-dev",
        **kwargs,
    )


def _version(text):
    return Conversation.TranscriptVersion(
        version_id="v1",
        transcript=text,
        created_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def conversations(mongo_service):
    """A real collection, isolated per run and torn down afterwards."""
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["chronicle_reconciler_test"]
    await init_beanie(database=database, document_models=[Conversation])
    await Conversation.find_all().delete()
    yield
    await client.drop_database("chronicle_reconciler_test")
    client.close()


async def _statuses() -> dict[str, str]:
    return {
        c.conversation_id: c.processing_status
        for c in await Conversation.find_all().to_list()
    }


@pytest.mark.integration
async def test_apply_status_still_routes_through_the_shared_rules(conversations):
    """Splitting the rules out must not give the model a second opinion."""
    conversation = _conversation("c1", processing_status=ACTIVE)

    assert conversation.apply_status(settled=True) is True
    assert (conversation.processing_status, conversation.failure_stage) == (
        FAILED,
        "transcription",
    )
    # Idempotent: a second pass changes nothing and says so.
    assert conversation.apply_status(settled=True) is False


@pytest.mark.integration
async def test_the_query_answers_has_transcript_the_way_the_model_does(conversations):
    """Every shape the text can take, since this is what moved into Mongo."""
    old = datetime.now(timezone.utc) - timedelta(days=2)

    await _conversation(
        "real",
        created_at=old,
        active_transcript_version="v1",
        transcript_versions=[_version("we talked about it")],
    ).insert()
    await _conversation(
        "empty",
        created_at=old,
        active_transcript_version="v1",
        transcript_versions=[_version("")],
    ).insert()
    await _conversation(
        "whitespace",
        created_at=old,
        active_transcript_version="v1",
        transcript_versions=[_version("   \n  ")],
    ).insert()
    await _conversation(
        "null-text",
        created_at=old,
        active_transcript_version="v1",
        transcript_versions=[_version(None)],
    ).insert()
    await _conversation("no-versions", created_at=old).insert()

    result = await reconcile_conversation_statuses()

    assert result["scanned"] == 5
    statuses = await _statuses()
    assert statuses["real"] == COMPLETED
    # An empty, whitespace-only, or absent transcript is not a deliverable.
    for cid in ("empty", "whitespace", "null-text", "no-versions"):
        assert statuses[cid] == FAILED, cid


@pytest.mark.integration
async def test_the_active_version_decides_not_merely_any_version(conversations):
    """A conversation re-transcribed to an empty version has not succeeded."""
    old = datetime.now(timezone.utc) - timedelta(days=2)
    good = Conversation.TranscriptVersion(
        version_id="v1", transcript="real words", created_at=old
    )
    blank = Conversation.TranscriptVersion(
        version_id="v2", transcript="", created_at=old
    )
    await _conversation(
        "points-at-blank",
        created_at=old,
        active_transcript_version="v2",
        transcript_versions=[good, blank],
    ).insert()
    await _conversation(
        "points-at-good",
        created_at=old,
        active_transcript_version="v1",
        transcript_versions=[good, blank],
    ).insert()

    await reconcile_conversation_statuses()

    statuses = await _statuses()
    assert statuses["points-at-blank"] == FAILED
    assert statuses["points-at-good"] == COMPLETED


@pytest.mark.integration
async def test_a_recent_conversation_without_a_transcript_is_still_in_flight(
    conversations,
):
    """The pipeline is probably still running; calling it failed would be wrong."""
    await _conversation("fresh", created_at=datetime.now(timezone.utc)).insert()
    await _conversation(
        "stale", created_at=datetime.now(timezone.utc) - timedelta(days=2)
    ).insert()

    await reconcile_conversation_statuses()

    statuses = await _statuses()
    assert statuses["fresh"] == ACTIVE
    assert statuses["stale"] == FAILED


@pytest.mark.integration
async def test_completed_at_settles_a_conversation_regardless_of_age(conversations):
    now = datetime.now(timezone.utc)
    await _conversation("finished", created_at=now, completed_at=now).insert()

    await reconcile_conversation_statuses()

    assert (await _statuses())["finished"] == FAILED


@pytest.mark.integration
async def test_deleted_conversations_are_left_alone(conversations):
    old = datetime.now(timezone.utc) - timedelta(days=2)
    await _conversation(
        "gone", created_at=old, deleted=True, processing_status=ACTIVE
    ).insert()

    result = await reconcile_conversation_statuses()

    assert result["scanned"] == 0
    assert (await _statuses())["gone"] == ACTIVE


@pytest.mark.integration
async def test_a_dry_run_reports_changes_without_writing_them(conversations):
    old = datetime.now(timezone.utc) - timedelta(days=2)
    await _conversation("drift", created_at=old, processing_status=ACTIVE).insert()

    result = await reconcile_conversation_statuses(dry_run=True)

    assert result["changed"] == 1
    assert result["details"][0]["to"] == FAILED
    assert (await _statuses())["drift"] == ACTIVE


@pytest.mark.integration
async def test_a_conversation_already_correct_is_not_rewritten(conversations):
    """The sweep is a backstop; on a healthy corpus it should write nothing."""
    old = datetime.now(timezone.utc) - timedelta(days=2)
    await _conversation(
        "settled",
        created_at=old,
        processing_status=FAILED,
        failure_stage="transcription",
    ).insert()

    result = await reconcile_conversation_statuses()

    assert result["changed"] == 0
