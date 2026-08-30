"""Settled-day episode memory: digest budgeting and the write latch."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from advanced_omi_backend.models.timeline import (
    TimelineAssertion,
    TimelineAudioRange,
    TimelineEvidenceRef,
    TimelineSemanticGroup,
)
from advanced_omi_backend.services.memory.base import DayWriteOutcome
from advanced_omi_backend.services.timeline import memory as memory_module
from advanced_omi_backend.services.timeline.memory import (
    _TERMINAL_MEMORY_STATES,
    _claim_query,
    build_day_digest,
    build_day_index_digest,
)
from advanced_omi_backend.services.timeline.projection import render_day_episode_index

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
    audio_conversation_ids: list[str] | None = None,
    assertions: list[TimelineAssertion] | None = None,
    kind: str | None = None,
    memory_policy: str = "auto",
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
        episode_key=f"key-{episode_id}",
        run_id="run-one",
        user_id="user-one",
        local_date=DAY,
        timezone=ZONE,
        started_at=started,
        ended_at=started + timedelta(minutes=minutes),
        kind=kind or ("meeting" if conversational else "work"),
        title=title,
        summary=summary,
        conversational=conversational,
        memory_policy=memory_policy,
        salience=salience,
        confidence=0.9,
        activity_mode="foreground",
        entities=[],
        attributes={},
        evidence_refs=refs,
        related_conversation_ids=related_conversation_ids or [],
        audio_ranges=(
            [
                TimelineAudioRange(
                    capture_source_id="capture-one",
                    time_basis="recorded",
                    chunk_ids=[f"chunk-{episode_id}"],
                    started_at=started,
                    ended_at=started + timedelta(minutes=minutes),
                    conversation_ids=list(audio_conversation_ids),
                )
            ]
            if audio_conversation_ids
            else []
        ),
        assertions=assertions or [],
    )


def test_digest_never_copies_raw_transcript_evidence_into_the_vault_prompt():
    episode = make_episode(
        "e1", hour=9, conversational=True, evidence_conversation_id="c1"
    )
    episode.evidence_refs[0].excerpt = "RAW TRANSCRIPT MUST STAY OUT OF THE VAULT"

    digest, dropped = build_day_digest([episode], DAY, ZONE)

    assert "RAW TRANSCRIPT" not in digest
    assert "transcript" not in digest.lower()
    assert dropped == []


def test_digest_renders_an_accepted_semantic_group_once_with_all_episode_keys():
    first = make_episode(
        "first", hour=9, title="Initial ASR check", summary="Checked live capture."
    )
    second = make_episode(
        "second", hour=10, title="VibeVoice check", summary="Checked music detection."
    )
    group = TimelineSemanticGroup(
        group_id="group-one",
        run_id="run-one",
        episode_ids=[first.episode_id, second.episode_id],
        episode_keys=[first.episode_key, second.episode_key],
        title="Live recording and ASR tests",
        summary="Two tests of the same live recording setup.",
        started_at=first.started_at,
        ended_at=second.ended_at,
    )

    digest, dropped = build_day_digest(
        [first, second], DAY, ZONE, semantic_groups=[group]
    )

    assert "1 reviewed semantic item(s) from 2 episode(s)" in digest
    assert digest.count("### Semantic group") == 1
    assert first.episode_key in digest
    assert second.episode_key in digest
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


def test_media_is_reference_only_by_default_and_uses_neutral_daily_wording():
    episode = make_episode(
        "media",
        hour=9,
        kind="media",
        title="Discussing a deal and doubts about fishing",
        summary="People discuss whether a deal is truthful.",
    )

    semantic_digest, dropped = build_day_digest([episode], DAY, ZONE)
    index_digest = build_day_index_digest([episode], DAY, ZONE)
    rendered_index = render_day_episode_index(index_digest)

    assert semantic_digest == ""
    assert dropped == []
    assert "Discussing a deal" not in index_digest
    assert "Media: a deal and doubts about fishing" in rendered_index
    assert "episode_key:key-media" in rendered_index


def test_media_content_reaches_semantic_memory_only_after_explicit_opt_in():
    episode = make_episode(
        "media",
        hour=9,
        kind="media",
        memory_policy="remember",
        title="Watching a documentary about octopuses",
        summary="The documentary explains octopus camouflage.",
    )

    digest, dropped = build_day_digest([episode], DAY, ZONE)

    assert dropped == []
    assert "Watching a documentary about octopuses" in digest
    assert "The documentary explains octopus camouflage" in digest


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


def test_day_index_keeps_every_range_when_semantic_digest_sheds_an_episode():
    episodes = [
        make_episode(
            "conversation",
            hour=9,
            conversational=True,
            title="Standup",
            summary="important " * 100,
        ),
        make_episode(
            "background",
            hour=10,
            salience="background",
            title="Background playback",
            summary="incidental " * 100,
        ),
    ]

    semantic_digest, dropped = build_day_digest(episodes, DAY, ZONE, max_chars=1_500)
    index_digest = build_day_index_digest(episodes, DAY, ZONE)

    assert dropped == ["Background playback"]
    assert "Background playback" not in semantic_digest
    assert "14:30–15:00" in index_digest
    assert "15:30–16:00" in index_digest
    assert "Standup" in index_digest
    assert "Background playback" in index_digest
    assert "important important" not in index_digest
    assert "incidental incidental" not in index_digest


def test_day_index_preserves_episode_and_authoritative_conversation_sources():
    episode = make_episode(
        "meeting",
        hour=9,
        conversational=True,
        related_conversation_ids=["lineage-only"],
        audio_conversation_ids=["canonical-conversation"],
    )

    semantic_digest, _ = build_day_digest([episode], DAY, ZONE)
    index_digest = build_day_index_digest([episode], DAY, ZONE)
    rendered_index = render_day_episode_index(index_digest)

    for digest in (semantic_digest, index_digest):
        assert "episode_key: key-meeting" in digest
        assert "canonical-conversation" in digest
        assert "lineage-only" in digest
    assert "[[Conversations/canonical-conversation|source]]" in rendered_index
    assert "[[Conversations/lineage-only|source]]" in rendered_index
    assert "<!-- episode_key:key-meeting -->" in rendered_index


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


def test_explicit_repair_claim_can_reclaim_partial_beyond_cron_attempt_budget():
    day = SimpleNamespace(
        user_id="user-one", local_date=DAY, timezone=ZONE, active_run_id="run-one"
    )

    query = _claim_query(
        day,
        claim_timeout_minutes=120,
        max_attempts=3,
        retry_partial=True,
    )

    assert "memory_attempts" not in query
    assert query["$or"][0]["memory_state"] == {"$in": ["", None, "partial"]}


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


@pytest.mark.asyncio
async def test_write_day_forwards_typed_timeline_source_identity(monkeypatch):
    episode = make_episode(
        "episode-one",
        hour=9,
        related_conversation_ids=["lineage-one"],
        audio_conversation_ids=["canonical-one"],
    )
    captured: dict = {}

    class FakeCollection:
        async def update_many(self, _query, _update):
            return None

    class FakeEpisodeCursor:
        async def to_list(self):
            return [episode]

    class FakeEpisodeModel:
        run_id = "run_id"
        user_id = "user_id"

        @staticmethod
        def find(*_args, **_kwargs):
            return FakeEpisodeCursor()

        @staticmethod
        def get_pymongo_collection():
            return FakeCollection()

    @contextmanager
    def capture_provenance(*args, **kwargs):
        captured["provenance"] = (args, kwargs)
        yield

    async def add_day_memory(*_args, **kwargs):
        captured["provider"] = kwargs
        return DayWriteOutcome.COMPLETE, []

    monkeypatch.setattr(memory_module, "TimelineEpisode", FakeEpisodeModel)
    monkeypatch.setattr(memory_module, "memory_provenance", capture_provenance)
    monkeypatch.setattr(
        memory_module,
        "get_memory_service",
        lambda: SimpleNamespace(add_day_memory=add_day_memory),
    )

    day = SimpleNamespace(
        user_id="user-one", local_date=DAY, timezone=ZONE, active_run_id="run-one"
    )
    assert await memory_module._write_day(day) == "no_changes"

    _args, provenance = captured["provenance"]
    assert provenance["source_type"] == "timeline_day"
    assert provenance["source_id"] == DAY.isoformat()
    assert provenance["source_episode_ids"] == ("episode-one",)
    assert provenance["source_conversation_ids"] == (
        "canonical-one",
        "lineage-one",
    )
    assert provenance["timeline_run_id"] == "run-one"
    assert captured["provider"]["source_run_id"] == "run-one"
    assert captured["provider"]["source_episode_ids"] == ["episode-one"]
    assert captured["provider"]["source_conversation_ids"] == [
        "canonical-one",
        "lineage-one",
    ]


@pytest.mark.asyncio
async def test_write_day_sends_default_media_only_to_the_reference_index(monkeypatch):
    episode = make_episode(
        "media",
        hour=9,
        kind="media",
        title="Discussing a deal and doubts about fishing",
        summary="People discuss whether a deal is truthful.",
    )
    captured: dict = {}

    class FakeCollection:
        async def update_many(self, _query, _update):
            return None

    class FakeEpisodeCursor:
        async def to_list(self):
            return [episode]

    class FakeEpisodeModel:
        run_id = "run_id"
        user_id = "user_id"

        @staticmethod
        def find(*_args, **_kwargs):
            return FakeEpisodeCursor()

        @staticmethod
        def get_pymongo_collection():
            return FakeCollection()

    async def add_day_memory(day_digest, *_args, **kwargs):
        captured["semantic"] = day_digest
        captured["index"] = kwargs["day_index_digest"]
        return DayWriteOutcome.COMPLETE, ["Daily/2026-08-06.md"]

    monkeypatch.setattr(memory_module, "TimelineEpisode", FakeEpisodeModel)
    monkeypatch.setattr(
        memory_module,
        "get_memory_service",
        lambda: SimpleNamespace(add_day_memory=add_day_memory),
    )

    day = SimpleNamespace(
        user_id="user-one", local_date=DAY, timezone=ZONE, active_run_id="run-one"
    )

    assert await memory_module._write_day(day) == "written"
    assert captured["semantic"] == ""
    assert "Discussing a deal" not in captured["index"]
    assert "Media: a deal and doubts about fishing" in captured["index"]


def _returns(value):
    async def _call(*_args, **_kwargs):
        return value

    return _call
