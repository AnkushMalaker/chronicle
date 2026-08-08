"""Settled-day episode memory: promotion, digest budgeting, and the write latch."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from advanced_omi_backend.models.timeline import TimelineAssertion, TimelineEvidenceRef
from advanced_omi_backend.services.timeline import discovery
from advanced_omi_backend.services.timeline.memory import (
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


def test_digest_includes_transcripts_only_for_conversational_episodes():
    episodes = [
        make_episode("e1", hour=9, conversational=True, evidence_conversation_id="c1"),
        make_episode("e2", hour=11, evidence_conversation_id="c2"),
    ]
    transcripts = {"c1": "daksh: morning standup", "c2": "should not appear"}

    digest, dropped = build_day_digest(episodes, DAY, ZONE, transcripts)

    assert "daksh: morning standup" in digest
    assert "should not appear" not in digest
    assert dropped == []


def test_digest_reads_a_naive_mongo_timestamp_as_utc_not_host_local():
    """Mongo returns naive datetimes. Treating them as host-local shifts every clock."""

    episode = make_episode("e1", hour=9)
    episode.started_at = episode.started_at.replace(tzinfo=None)
    episode.ended_at = episode.ended_at.replace(tzinfo=None)

    digest, _ = build_day_digest([episode], DAY, ZONE, {})

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

    digest, _ = build_day_digest([episode], DAY, ZONE, {})

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
    digest, dropped = build_day_digest(episodes, DAY, ZONE, {}, max_chars=1400)

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

    digest, dropped = build_day_digest(episodes, DAY, ZONE, {}, max_chars=2_000)

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

    _, dropped = build_day_digest(episodes, DAY, ZONE, {}, max_chars=200)

    assert len(dropped) == 4


def test_claim_query_excludes_terminal_states_and_exhausted_attempts():
    day = SimpleNamespace(user_id="user-one", local_date=DAY, timezone=ZONE)

    query = _claim_query(day, claim_timeout_minutes=120, max_attempts=3)

    # $not/$gte rather than $lt, so a day analysed before memory_attempts existed —
    # which has no such field — is still claimable. $lt never matches a missing field.
    assert query["memory_attempts"] == {"$not": {"$gte": 3}}
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
    enqueued: list[str] = []

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
    monkeypatch.setattr(
        discovery.default_queue,
        "enqueue",
        lambda _job, conversation_id, **_kwargs: enqueued.append(conversation_id),
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
    # Title/summary only — memory comes from the settled-day pass, not this chain.
    assert enqueued == ["screenpipe-meeting"]


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


def test_digest_trims_transcripts_before_it_drops_any_episode():
    """Transcript bulk must never cost the day its other episodes.

    A summary is a few hundred characters and a transcript tens of thousands, so
    shedding episodes to make room for transcripts throws away most of the day to save
    almost nothing. One measured day dropped 9 of its 13 episodes and then had to trim
    the transcripts anyway, leaving the agent to summarise "all four episodes".
    """

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

    digest, dropped = build_day_digest(
        episodes, DAY, ZONE, {"c1": "a" * 40_000}, max_chars=5_000
    )

    assert len(digest) <= 5_000
    assert all("trimmed" in item for item in dropped)
    assert "Standup" in digest
    for index in range(6):
        assert f"Background {index}" in digest


def test_digest_trims_transcripts_when_conversational_episodes_exceed_budget():
    """The budget must hold even when every episode is undroppable.

    Conversational episodes are never dropped, so a day whose transcripts alone exceed
    the budget used to ship at whatever size it happened to be — which is how a digest
    reached the write agent at twice the model's context.
    """

    episodes = [
        make_episode(
            "e1",
            hour=9,
            conversational=True,
            title="Standup",
            evidence_conversation_id="c1",
        ),
        make_episode(
            "e2",
            hour=11,
            conversational=True,
            title="One-on-one",
            evidence_conversation_id="c2",
        ),
    ]
    transcripts = {"c1": "a" * 40_000, "c2": "b" * 40_000}

    digest, dropped = build_day_digest(
        episodes, DAY, ZONE, transcripts, max_chars=5_000
    )

    assert len(digest) <= 5_000
    assert any("trimmed" in item for item in dropped)
    # Both conversations survive as content; only their tails are cut.
    assert "Standup" in digest and "One-on-one" in digest
