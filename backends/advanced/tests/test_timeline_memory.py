"""Settled-day episode memory: promotion, digest budgeting, and the write latch."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from advanced_omi_backend.models.timeline import TimelineAssertion, TimelineEvidenceRef
from advanced_omi_backend.services.memory.base import DayWriteOutcome
from advanced_omi_backend.services.timeline import discovery
from advanced_omi_backend.services.timeline import memory as memory_module
from advanced_omi_backend.services.timeline.memory import (
    _TERMINAL_MEMORY_STATES,
    _cited_conversation_ids,
    _claim_query,
    build_day_digest,
)

DAY = date(2026, 8, 6)
ZONE = "Asia/Kolkata"


# Beanie Documents cannot be constructed without an initialized collection, so these
# stand in for TimelineEpisode/TimelineDay the same way test_timeline_routes.py does.
def make_episode(
    episode_id: str,
    *,
    hour: int,
    conversational: bool = False,
    salience: str = "routine",
    title: str = "Episode",
    summary: str = "",
    minutes: int = 30,
    related_conversation_ids: list[str] | None = None,
    evidence_conversation_id: str | None = None,
    assertions: list[TimelineAssertion] | None = None,
) -> SimpleNamespace:
    started = datetime(2026, 8, 6, hour, 0, tzinfo=timezone.utc)
    refs = [
        TimelineEvidenceRef(
            evidence_id=f"transcript:{episode_id}",
            kind="transcript",
            started_at=started,
            ended_at=started + timedelta(minutes=minutes),
            role="uncertain",
            metadata=(
                {"conversation_id": evidence_conversation_id}
                if evidence_conversation_id
                else {}
            ),
        )
    ]
    return SimpleNamespace(
        episode_id=episode_id,
        run_id="run-one",
        user_id="user-one",
        local_date=DAY,
        timezone=ZONE,
        started_at=started,
        ended_at=started + timedelta(minutes=minutes),
        kind="meeting" if conversational else "work",
        title=title,
        summary=summary,
        conversational=conversational,
        salience=salience,
        confidence=0.9,
        activity_mode="foreground",
        entities=[],
        attributes={},
        evidence_refs=refs,
        related_conversation_ids=related_conversation_ids or [],
        assertions=assertions or [],
    )


def test_cited_ids_union_agent_named_and_assembly_recorded():
    episode = make_episode(
        "e1",
        hour=9,
        related_conversation_ids=["agent-named"],
        evidence_conversation_id="assembly-recorded",
    )

    assert _cited_conversation_ids(episode) == {"agent-named", "assembly-recorded"}


def test_digest_never_copies_raw_transcript_evidence_into_the_vault_prompt():
    episode = make_episode(
        "e1", hour=9, conversational=True, evidence_conversation_id="c1"
    )
    episode.evidence_refs[0].excerpt = "RAW TRANSCRIPT MUST STAY OUT OF THE VAULT"

    digest, dropped = build_day_digest([episode], DAY, ZONE)

    assert "RAW TRANSCRIPT" not in digest
    assert "transcript" not in digest.lower()
    assert dropped == []


def test_digest_reads_a_naive_mongo_timestamp_as_utc_not_host_local():
    """Mongo returns naive datetimes. Treating them as host-local shifts every clock."""

    episode = make_episode("e1", hour=9)
    episode.started_at = episode.started_at.replace(tzinfo=None)
    episode.ended_at = episode.ended_at.replace(tzinfo=None)

    digest, _ = build_day_digest([episode], DAY, ZONE)

    # 09:00 UTC is 14:30 in Asia/Kolkata regardless of where the backend runs.
    assert "14:30–15:00" in digest


def test_digest_records_assertion_role_and_confidence():
    episode = make_episode(
        "e1",
        hour=9,
        assertions=[
            TimelineAssertion(
                claim="A podcast played",
                role="media_content",
                confidence=0.4,
                evidence_ids=["transcript:e1"],
            )
        ],
    )

    digest, _ = build_day_digest([episode], DAY, ZONE)

    # Role and confidence are what stop media dialogue being recorded as user fact.
    assert "media_content" in digest
    assert "0.40" in digest


def test_digest_sheds_lowest_salience_first_and_never_the_conversation():
    padding = "x" * 400
    episodes = [
        make_episode(
            "e1", hour=9, conversational=True, title="Standup", summary=padding
        ),
        make_episode(
            "e2", hour=10, salience="background", title="Idle music", summary=padding
        ),
        make_episode(
            "e3",
            hour=11,
            salience="highlight",
            title="Shipped release",
            summary=padding,
        ),
    ]

    # Room for the conversation plus one more.
    digest, dropped = build_day_digest(episodes, DAY, ZONE, max_chars=1400)

    # Background is shed before highlight, and a conversation is never droppable.
    assert dropped == ["Idle music"]
    assert "Idle music" not in digest
    assert "Standup" in digest
    assert "Shipped release" in digest


def test_digest_header_states_the_count_it_actually_carries():
    """A header claiming 13 above a body of 4 is a digest that lies to the model."""

    padding = "y" * 800
    episodes = [
        make_episode(
            f"e{index}",
            hour=9 + index,
            salience="background",
            title=f"Thing {index}",
            summary=padding,
        )
        for index in range(4)
    ]

    digest, dropped = build_day_digest(episodes, DAY, ZONE, max_chars=2_000)

    header = digest.splitlines()[0]
    included = sum(1 for line in digest.splitlines() if line.startswith("### "))
    assert len(dropped) > 0
    assert f"{included} of 4 episode(s)" in header
    assert "omitted to fit" in header


def test_digest_reports_every_dropped_episode_rather_than_truncating_silently():
    padding = "y" * 800
    episodes = [
        make_episode(
            f"e{index}", hour=9 + index, salience="background", summary=padding
        )
        for index in range(4)
    ]

    _, dropped = build_day_digest(episodes, DAY, ZONE, max_chars=200)

    assert len(dropped) == 4


def test_claim_query_excludes_terminal_states_and_exhausted_attempts():
    day = SimpleNamespace(
        user_id="user-one", local_date=DAY, timezone=ZONE, active_run_id="run-one"
    )

    query = _claim_query(day, claim_timeout_minutes=120, max_attempts=3)

    # $not/$gte rather than $lt, so a day analysed before memory_attempts existed —
    # which has no such field — is still claimable. $lt never matches a missing field.
    assert query["memory_attempts"] == {"$not": {"$gte": 3}}
    assert query["active_run_id"] == "run-one"
    states = query["$or"]
    # Only an unwritten day, or one whose claim has aged out, is claimable. "written"
    # and "skipped" must never match: the vault already holds the day, and rewriting
    # after a non-deterministic re-analysis would record it twice.
    assert states[0]["memory_state"] == {"$in": ["", None]}
    assert states[1]["memory_state"] == "claimed"
    assert "$lt" in states[1]["memory_claimed_at"]


@pytest.mark.asyncio
async def test_promotion_only_touches_cited_capture_evidence(monkeypatch):
    """A conversational episode un-fences its recordings; other episodes do not."""

    updated: dict = {}
    enqueued: list[tuple[str, dict]] = []

    class FakeCursor:
        def __init__(self, documents):
            self._documents = documents

        def __aiter__(self):
            async def generate():
                for document in self._documents:
                    yield document

            return generate()

    class FakeCollection:
        def __init__(self):
            self.queries = []

        def find(self, query, _projection=None):
            self.queries.append(query)
            wanted = set(query["conversation_id"]["$in"])
            # Only "screenpipe-meeting" is still capture evidence in this fixture.
            return FakeCursor(
                [{"conversation_id": "screenpipe-meeting"}]
                if "screenpipe-meeting" in wanted
                else []
            )

        async def update_many(self, query, update):
            updated["query"] = query
            updated["update"] = update

    collection = FakeCollection()
    monkeypatch.setattr(
        discovery.Conversation, "get_pymongo_collection", lambda: collection
    )
    # This test is about which recordings promotion selects. Resolving a cited id to
    # whatever is live now needs a real database and is covered by
    # tests/test_recording_refs.py.
    monkeypatch.setattr(discovery, "resolve_live_recordings", _identity)
    monkeypatch.setattr(
        discovery,
        "enqueue_summary_job_bundle",
        lambda conversation_id, **kwargs: enqueued.append((conversation_id, kwargs)),
    )

    promoted = await discovery._promote_conversational_recordings(
        [
            make_episode(
                "meeting",
                hour=9,
                conversational=True,
                evidence_conversation_id="screenpipe-meeting",
            ),
            make_episode("gaming", hour=11, evidence_conversation_id="ambient-noise"),
        ]
    )

    assert promoted == ["screenpipe-meeting"]
    # The non-conversational episode's recording is never even considered.
    assert collection.queries[0]["conversation_id"]["$in"] == ["screenpipe-meeting"]
    assert collection.queries[0]["data_purpose"] == "capture_evidence"
    assert updated["update"]["$set"] == {
        "data_purpose": "conversation",
        "memory_excluded": False,
        "memory_exclusion_reason": None,
    }
    # The complete summary bundle runs, but memory comes from the settled-day pass.
    assert enqueued == [
        (
            "screenpipe-meeting",
            {
                "include_memory_context": False,
                "meta": {
                    "conversation_id": "screenpipe-meeting",
                    "trigger": "timeline_promotion",
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_promotion_is_a_noop_without_a_conversational_episode(monkeypatch):
    def explode():
        raise AssertionError("must not query conversations")

    monkeypatch.setattr(discovery.Conversation, "get_pymongo_collection", explode)

    assert (
        await discovery._promote_conversational_recordings(
            [make_episode("gaming", hour=11, evidence_conversation_id="ambient")]
        )
        == []
    )


def test_digest_keeps_all_episode_summaries_when_they_fit():

    episodes = [
        make_episode(
            "talk",
            hour=9,
            conversational=True,
            title="Standup",
            evidence_conversation_id="c1",
        ),
        *(
            make_episode(
                f"bg{index}",
                hour=10 + index,
                salience="background",
                title=f"Background {index}",
            )
            for index in range(6)
        ),
    ]

    digest, dropped = build_day_digest(episodes, DAY, ZONE, max_chars=5_000)

    assert len(digest) <= 5_000
    assert dropped == []
    assert "Standup" in digest
    for index in range(6):
        assert f"Background {index}" in digest


def test_digest_drops_no_conversational_episode_when_summaries_exceed_budget():
    """Conversation summaries stay; raw transcripts never enter this budget."""

    episodes = [
        make_episode(
            "e1",
            hour=9,
            conversational=True,
            title="Standup",
            evidence_conversation_id="c1",
            summary="a" * 4_000,
        ),
        make_episode(
            "e2",
            hour=11,
            conversational=True,
            title="One-on-one",
            evidence_conversation_id="c2",
            summary="b" * 4_000,
        ),
    ]
    digest, dropped = build_day_digest(episodes, DAY, ZONE, max_chars=5_000)

    assert dropped == []
    assert "Standup" in digest and "One-on-one" in digest


async def _identity(conversation_ids):
    return set(conversation_ids)


def test_partial_is_terminal_so_a_truncated_day_is_not_re_attempted():
    """A truncated write must be neither retried nor reported as written.

    ``partial`` exists because a boolean forced a cut-off run to be mislabelled: as
    ``written`` it claims People/Topic edits the run may never have reached, and as a
    failure it burns two more agent runs re-reaching the same round limit before
    settling as ``skipped`` — which says there was nothing to record.

    Both selectors have to agree it is terminal. The settled-day scan is what would
    re-pick it on the next tick, and it is a separate expression from ``_claim_query``,
    so it is the one that silently regresses.
    """

    assert "partial" in _TERMINAL_MEMORY_STATES
    assert set(_TERMINAL_MEMORY_STATES) == {
        "written",
        "partial",
        "skipped",
        "no_changes",
    }

    day = SimpleNamespace(
        user_id="user-one", local_date=DAY, timezone=ZONE, active_run_id="run-one"
    )
    query = _claim_query(day, claim_timeout_minutes=120, max_attempts=3)
    # _claim_query admits only an unset or aged-out claim, so "partial" cannot match
    # either arm.
    claimable = query["$or"]
    assert claimable[0]["memory_state"] == {"$in": ["", None]}
    assert claimable[1]["memory_state"] == "claimed"


@pytest.mark.asyncio
async def test_write_day_records_a_truncated_run_as_partial(monkeypatch):
    """The real entry point maps the provider's outcome, not a re-derived guess."""

    episode = make_episode("episode-one", hour=9)
    recorded: dict = {}

    class FakeCollection:
        async def update_many(self, _query, update):
            recorded.update(update["$set"])

    class FakeEpisodeCursor:
        async def to_list(self):
            return [episode]

    # Stand in for the Beanie Document entirely: building the real query touches
    # TimelineEpisode.run_id as a class attribute, which raises without an
    # initialized collection.
    class FakeEpisodeModel:
        run_id = "run_id"
        user_id = "user_id"

        @staticmethod
        def find(*_args, **_kwargs):
            return FakeEpisodeCursor()

        @staticmethod
        def get_pymongo_collection():
            return FakeCollection()

    monkeypatch.setattr(memory_module, "TimelineEpisode", FakeEpisodeModel)
    monkeypatch.setattr(
        memory_module,
        "get_memory_service",
        lambda: SimpleNamespace(
            add_day_memory=_returns((DayWriteOutcome.PARTIAL, ["Daily/2026-08-06.md"]))
        ),
    )

    day = SimpleNamespace(
        user_id="user-one", local_date=DAY, timezone=ZONE, active_run_id="run-one"
    )
    outcome = await memory_module._write_day(day)

    assert outcome == "partial"
    # The episodes carry the same honest label, so provenance does not claim a
    # complete write either.
    assert recorded["memory_state"] == "partial"
    assert recorded["vault_paths"] == ["Daily/2026-08-06.md"]


def _returns(value):
    async def _call(*_args, **_kwargs):
        return value

    return _call
