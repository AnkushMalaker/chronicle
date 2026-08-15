"""Rolling reconciliation: budgets, pinned boundaries, fencing, lineage, settlement.

Real MongoDB documents, because the parts worth testing here are conditional updates
and revision lineage — a faked collection would be testing the fake. The agent is a
scripted stub returning ``ReconcileAction``s, and evidence is supplied directly, so no
LLM and no evidence assembly are involved.
"""

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import (
    AudioEvidenceSpan,
    DirtyEvidenceRange,
    TimelineDay,
    TimelineEpisode,
)
from advanced_omi_backend.services.timeline import reconciliation
from advanced_omi_backend.services.timeline.contracts import (
    AgentEpisode,
    EvidenceBundle,
    Publish,
    RequestMoreContext,
    TimelineAgentResult,
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
    WaitForFutureEvidence,
)
from advanced_omi_backend.services.timeline.reconciliation import (
    assess_settlement,
    observed_revisions,
    publish_reconciliation,
    reconcile_range,
)
from advanced_omi_backend.workers import timeline_jobs

USER = "user-reconcile"
START = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@pytest.fixture
async def documents(mongo_service, monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_reconciliation_db"]
    await init_beanie(
        database=database,
        document_models=[
            DirtyEvidenceRange,
            TimelineEpisode,
            TimelineDay,
            Conversation,
            AudioEvidenceSpan,
        ],
    )
    for model in (DirtyEvidenceRange, TimelineEpisode, Conversation, AudioEvidenceSpan):
        await model.delete_all()
    # The user row is not part of this suite; UTC is the documented fallback.
    monkeypatch.setattr(reconciliation, "_user_timezone", _always_utc)
    yield database
    await client.drop_database("test_reconciliation_db")
    client.close()


async def _always_utc(_user_id: str) -> str:
    return "UTC"


# ── Builders ─────────────────────────────────────────────────────────────────


def evidence_item(offset_minutes: float, minutes: float, index: int = 1):
    start = START + timedelta(minutes=offset_minutes)
    return TimelineEvidenceItem(
        evidence_id=f"transcript:{index}",
        kind="transcript",
        started_at=start,
        ended_at=start + timedelta(minutes=minutes),
        role="user_statement",
        excerpt="hello",
    )


def make_bundle(
    *,
    bounds=(0, 60),
    items=None,
    existing=None,
    pinned=None,
    evidence_revision=1,
) -> EvidenceBundle:
    items = [evidence_item(10, 10)] if items is None else items
    started_at = START + timedelta(minutes=bounds[0])
    ended_at = START + timedelta(minutes=bounds[1])
    manifest = TimelineEvidenceManifest(
        user_id=USER,
        local_date=date(2026, 8, 15),
        timezone="UTC",
        started_at=started_at,
        ended_at=ended_at,
        evidence_revision="hash-1",
        windows=[
            TimelineCoverageWindow(
                window_id="w1",
                started_at=started_at,
                ended_at=ended_at,
                evidence_ids=[item.evidence_id for item in items],
            )
        ],
        evidence=items,
    )
    return EvidenceBundle(
        manifest=manifest,
        existing_episodes=existing or [],
        pinned_episodes=pinned or [],
        evidence_revision=evidence_revision,
    )


def agent_episode(
    offset_minutes: float,
    minutes: float,
    *,
    title="Talking",
    evidence_ids=("transcript:1",),
) -> AgentEpisode:
    start = START + timedelta(minutes=offset_minutes)
    return AgentEpisode(
        kind="chat",
        title=title,
        summary="a conversation",
        started_at=start,
        ended_at=start + timedelta(minutes=minutes),
        conversational=True,
        salience="routine",
        activity_mode="foreground",
        confidence=0.8,
        evidence_ids=list(evidence_ids),
    )


def publish_of(*episodes) -> Publish:
    return Publish(result=TimelineAgentResult(episodes=list(episodes)))


class ScriptedExecutor:
    """Returns the next scripted action; repeats the last one forever."""

    def __init__(self, *actions):
        self.actions = list(actions)
        self.calls: list[str | None] = []

    async def reconcile(self, bundle, *, validation_feedback=None, **_kwargs):
        self.calls.append(validation_feedback)
        index = min(len(self.calls) - 1, len(self.actions) - 1)
        return self.actions[index]


class RecordingProjection:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    async def __call__(self, user_id, dates=None, **_kwargs):
        self.calls.append((user_id, list(dates or [])))
        return list(dates or [])


async def make_dirty_range(*, offset_minutes=0, minutes=30, revision=7):
    row = DirtyEvidenceRange(
        user_id=USER,
        started_at=START + timedelta(minutes=offset_minutes),
        ended_at=START + timedelta(minutes=offset_minutes + minutes),
        evidence_revision=revision,
        leased_evidence_revision=revision,
        not_before=START,
        force_after=START,
        state="leased",
    )
    await row.insert()
    return row


async def make_prior_episode(
    *,
    offset_minutes=0,
    minutes=60,
    revision=1,
    title="Talking",
    evidence_revision=5,
) -> TimelineEpisode:
    start = START + timedelta(minutes=offset_minutes)
    episode = TimelineEpisode(
        run_id="rolling:previous",
        user_id=USER,
        local_date=date(2026, 8, 15),
        timezone="UTC",
        started_at=start,
        ended_at=start + timedelta(minutes=minutes),
        kind="chat",
        title=title,
        summary="a conversation",
        conversational=True,
        status="provisional",
        pipeline="rolling",
        revision=revision,
        evidence_revision=evidence_revision,
        salience="routine",
        confidence=0.8,
        activity_mode="foreground",
    )
    await episode.insert()
    return episode


def existing_payload(episode: TimelineEpisode) -> dict:
    return {
        "episode_id": episode.episode_id,
        "started_at": _utc(episode.started_at).isoformat(),
        "ended_at": _utc(episode.ended_at).isoformat(),
        "kind": episode.kind,
        "title": episode.title,
        "summary": episode.summary,
    }


def evidence_loader(bundle: EvidenceBundle, sink: list | None = None):
    async def load(_user_id, started_at, ended_at, **_kwargs):
        if sink is not None:
            sink.append((_utc(started_at), _utc(ended_at)))
        return bundle

    return load


# ── Context expansion budgets ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seventh_expansion_request_parks_the_range(documents):
    dirty = await make_dirty_range()
    executor = ScriptedExecutor(
        RequestMoreContext(left_seconds=300, right_seconds=0, reason="ambiguous start")
    )
    seen: list[tuple[datetime, datetime]] = []
    accounting: list[dict] = []

    outcome = await reconcile_range(
        dirty,
        executor=executor,
        load_evidence=evidence_loader(make_bundle(), seen),
        refresh_projections=RecordingProjection(),
        accounting=accounting,
    )

    assert outcome is None
    assert (
        len(executor.calls) == 7
    ), "six expansions are granted, the seventh is refused"
    assert len(seen) == 7, "each granted expansion reloads evidence exactly once"
    assert len(accounting) == 7
    # Six 5-minute steps on the requested side only.
    assert seen[-1][0] == _utc(dirty.started_at) - timedelta(minutes=5 + 30)
    assert seen[-1][1] == _utc(dirty.ended_at) + timedelta(minutes=5)
    refreshed = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert refreshed.state == "waiting"
    assert any("expansion budget" in reason for reason in refreshed.trigger_reasons)


@pytest.mark.asyncio
async def test_oversized_expansion_request_is_clamped_to_one_step(documents):
    dirty = await make_dirty_range()
    executor = ScriptedExecutor(
        RequestMoreContext(left_seconds=1200, right_seconds=0, reason="needs history"),
        publish_of(agent_episode(10, 10)),
    )
    seen: list[tuple[datetime, datetime]] = []

    await reconcile_range(
        dirty,
        executor=executor,
        load_evidence=evidence_loader(make_bundle(), seen),
        refresh_projections=RecordingProjection(),
    )

    assert seen[1][0] == seen[0][0] - timedelta(
        minutes=5
    ), "20 minutes asked, 5 granted"
    assert seen[1][1] == seen[0][1], "the unrequested side is never widened"


@pytest.mark.asyncio
async def test_wait_for_future_evidence_parks_without_publishing(documents):
    dirty = await make_dirty_range()
    executor = ScriptedExecutor(WaitForFutureEvidence(reason="recording still open"))

    outcome = await reconcile_range(
        dirty,
        executor=executor,
        load_evidence=evidence_loader(make_bundle()),
        refresh_projections=RecordingProjection(),
    )

    assert outcome is None
    assert await TimelineEpisode.find_all().count() == 0
    refreshed = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert refreshed.state == "waiting"
    assert "waiting:recording still open" in refreshed.trigger_reasons


@pytest.mark.asyncio
async def test_episode_crossing_a_pinned_boundary_is_retried_then_parked(documents):
    dirty = await make_dirty_range()
    pinned = [
        {
            "episode_key": "pin-1",
            "started_at": (START + timedelta(minutes=15)).isoformat(),
            "ended_at": (START + timedelta(minutes=45)).isoformat(),
            "kind": "meeting",
            "title": "Standup",
            "summary": "pinned by a person",
        }
    ]
    executor = ScriptedExecutor(publish_of(agent_episode(10, 10)))

    outcome = await reconcile_range(
        dirty,
        executor=executor,
        load_evidence=evidence_loader(make_bundle(pinned=pinned)),
        refresh_projections=RecordingProjection(),
    )

    assert outcome is None
    assert len(executor.calls) == 2, "one validation-feedback retry, then park"
    assert executor.calls[0] is None
    assert "pinned boundary" in (executor.calls[1] or "")
    assert await TimelineEpisode.find_all().count() == 0
    refreshed = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert refreshed.state == "waiting"


# ── Settlement policy ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_episode_at_the_live_edge_is_open(documents):
    bundle = make_bundle(items=[evidence_item(10, 10)])
    episode = agent_episode(10, 10)

    status = await assess_settlement(
        episode, bundle, START + timedelta(minutes=25), user_id=USER
    )

    assert status == "open"


@pytest.mark.asyncio
async def test_quiet_boundary_without_pending_work_settles(documents):
    bundle = make_bundle(
        items=[evidence_item(10, 10), evidence_item(40, 10, index=2)],
    )
    episode = agent_episode(10, 10)

    status = await assess_settlement(
        episode, bundle, START + timedelta(minutes=60), user_id=USER
    )

    assert status == "settled"


@pytest.mark.asyncio
async def test_unscored_audio_span_keeps_the_episode_provisional(documents):
    await AudioEvidenceSpan(
        user_id=USER,
        source_id="screenpipe",
        source_item_ids=["a"],
        first_source_item_id="a",
        last_source_item_id="a",
        source_range_hash="hash",
        started_at=START + timedelta(minutes=12),
        ended_at=START + timedelta(minutes=18),
        state="unscored",
        covered_seconds=360,
        missing_seconds=0,
    ).insert()
    bundle = make_bundle(items=[evidence_item(10, 10), evidence_item(40, 10, index=2)])

    status = await assess_settlement(
        agent_episode(10, 10), bundle, START + timedelta(minutes=60), user_id=USER
    )

    assert status == "provisional"


# ── Publishing: fencing, lineage, projections ────────────────────────────────


@pytest.mark.asyncio
async def test_stale_run_cannot_publish_over_a_newer_revision(documents):
    dirty = await make_dirty_range()
    prior = await make_prior_episode()
    bundle = make_bundle(existing=[existing_payload(prior)])
    snapshot = await observed_revisions(bundle)
    assert snapshot == {prior.episode_id: 1}

    # A concurrent generation lands while this run was thinking.
    await TimelineEpisode.get_pymongo_collection().update_one(
        {"episode_id": prior.episode_id}, {"$set": {"revision": 2}}
    )
    projection = RecordingProjection()

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        TimelineAgentResult(episodes=[agent_episode(10, 10, title="Revised")]),
        observed=snapshot,
        timezone_name="UTC",
        refresh_projections_fn=projection,
    )

    assert outcome.fenced is False
    assert outcome.material_change is False
    assert await TimelineEpisode.find_all().count() == 1, "no new row was written"
    assert projection.calls == []


@pytest.mark.asyncio
async def test_untouched_episode_carries_forward_without_writing(documents):
    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=10, minutes=10)
    bundle = make_bundle(existing=[existing_payload(prior)])
    projection = RecordingProjection()

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        TimelineAgentResult(episodes=[agent_episode(10, 10)]),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
        refresh_projections_fn=projection,
    )

    assert outcome.fenced is True
    assert outcome.material_change is False
    assert await TimelineEpisode.find_all().count() == 1
    survivor = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == prior.episode_id
    )
    assert (survivor.status, survivor.revision) == ("provisional", 1)
    assert projection.calls == []


@pytest.mark.asyncio
async def test_revised_episode_keeps_its_key_and_increments_its_revision(documents):
    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=10, minutes=10)
    bundle = make_bundle(existing=[existing_payload(prior)])
    projection = RecordingProjection()

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        TimelineAgentResult(episodes=[agent_episode(10, 12, title="Longer chat")]),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
        refresh_projections_fn=projection,
    )

    assert outcome.fenced and outcome.material_change
    assert outcome.episode_keys == [prior.episode_key]
    published = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == outcome.episode_ids[0]
    )
    assert published.revision == 2
    assert published.pipeline == "rolling"
    assert published.evidence_revision == dirty.leased_evidence_revision
    superseded = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == prior.episode_id
    )
    assert superseded.status == "superseded"
    # A same-key revision resolves by highest revision, so it needs no pointer.
    assert superseded.successor_keys == []
    assert projection.calls == [(USER, [date(2026, 8, 15)])]


@pytest.mark.asyncio
async def test_split_records_lineage_on_both_sides(documents):
    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=10, minutes=30)
    bundle = make_bundle(
        items=[evidence_item(10, 10), evidence_item(25, 15, index=2)],
        existing=[existing_payload(prior)],
    )

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        TimelineAgentResult(
            episodes=[
                agent_episode(10, 10, title="First half"),
                agent_episode(
                    25, 15, title="Second half", evidence_ids=("transcript:2",)
                ),
            ]
        ),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
        refresh_projections_fn=RecordingProjection(),
    )

    assert len(outcome.episode_keys) == 2
    assert prior.episode_key not in outcome.episode_keys, "a split mints new keys"
    for episode_id in outcome.episode_ids:
        row = await TimelineEpisode.find_one(TimelineEpisode.episode_id == episode_id)
        assert row.predecessor_keys == [prior.episode_key]
        assert row.revision == 1
    superseded = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == prior.episode_id
    )
    assert superseded.status == "superseded"
    assert sorted(superseded.successor_keys) == sorted(outcome.episode_keys)


@pytest.mark.asyncio
async def test_cross_midnight_episode_affects_both_local_dates(documents):
    dirty = await make_dirty_range()
    midnight = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    item = TimelineEvidenceItem(
        evidence_id="transcript:1",
        kind="transcript",
        started_at=midnight - timedelta(minutes=10),
        ended_at=midnight + timedelta(minutes=10),
        role="user_statement",
    )
    bundle = make_bundle(items=[item])
    bundle.manifest.started_at = midnight - timedelta(hours=1)
    bundle.manifest.ended_at = midnight + timedelta(hours=1)
    episode = agent_episode(0, 1)
    episode.started_at = midnight - timedelta(minutes=10)
    episode.ended_at = midnight + timedelta(minutes=10)
    projection = RecordingProjection()

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        TimelineAgentResult(episodes=[episode]),
        observed={},
        timezone_name="UTC",
        refresh_projections_fn=projection,
    )

    assert outcome.affected_local_dates == [date(2026, 8, 15), date(2026, 8, 16)]
    assert projection.calls == [(USER, [date(2026, 8, 15), date(2026, 8, 16)])]


# ── The RQ entry point ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_range_job_runs_the_registered_entry_point(
    documents, monkeypatch
):
    dirty = await make_dirty_range()
    await DirtyEvidenceRange.get_pymongo_collection().update_one(
        {"dirty_range_id": dirty.dirty_range_id},
        {"$set": {"state": "pending", "lease_owner": None}},
    )
    executor = ScriptedExecutor(publish_of(agent_episode(10, 10)))
    bundle = make_bundle()
    monkeypatch.setattr(
        reconciliation, "build_range_executor", lambda: executor, raising=True
    )
    monkeypatch.setattr(
        reconciliation, "load_reconciliation_evidence", evidence_loader(bundle)
    )
    monkeypatch.setattr(reconciliation, "refresh_projections", RecordingProjection())

    result = await timeline_jobs.reconcile_range_job.__wrapped__(dirty.dirty_range_id)

    assert result["state"] == "completed"
    assert result["material_change"] is True
    assert len(result["published"]) == 1
    assert result["iterations"][0]["evidence_count"] == 1
    stored = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert stored.state == "completed"
    assert stored.lease_owner is None
    assert await TimelineEpisode.find_all().count() == 1
