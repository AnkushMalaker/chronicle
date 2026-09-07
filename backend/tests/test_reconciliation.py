"""Rolling reconciliation: budgets, pinned boundaries, fencing, lineage, settlement.

Real MongoDB documents, because the parts worth testing here are conditional updates
and revision lineage — a faked collection would be testing the fake. The agent is a
scripted stub returning ``ReconcileAction``s, and evidence is supplied directly, so no
LLM and no evidence assembly are involved.
"""

import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.conversation import Conversation
from backend.models.device_input import DeviceInputJob
from backend.models.timeline import (
    AudioEvidenceSpan,
    DirtyEvidenceRange,
    DirtyEvidenceRangeResolution,
)
from backend.models.timeline import EpisodeRevisionRef as SnapshotEpisodeRevisionRef
from backend.models.timeline import (
    EvidenceAnchor,
    EvidenceLocator,
    GroupRevisionRef,
    TimelineDay,
    TimelineEpisode,
    TimelinePublicationDayPlan,
    TimelinePublicationJournal,
    TimelineReconciliationRequest,
    TimelineSemanticGroupRevision,
)
from backend.redis_keys import timeline_evidence_revision
from backend.routers.modules.timeline_routes import (
    NotActivityRequest,
    reject_timeline_activity,
)
from backend.services.timeline import dirty_ranges, publication, reconciliation
from backend.services.timeline.contracts import (
    AgentEpisode,
    EpisodeLineageProposal,
    EpisodeRevisionRef,
    EvidenceBundle,
    InterpretationResult,
    InterpretedEpisode,
    Publish,
    PublishResult,
    RejectedHypothesis,
    RequestMoreContext,
    SeparatedEpisode,
    SeparationResult,
    StageContextRequest,
    StageInferenceProvenance,
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
    ValidatedTimelineProjection,
    WaitForFutureEvidence,
)
from backend.services.timeline.reconciliation import (
    assess_settlement,
    observed_revisions,
    publish_reconciliation,
    reconcile_range,
)
from backend.services.timeline.snapshots import build_day_snapshot
from backend.workers import timeline_jobs


def _stage_provenance(stage: str) -> StageInferenceProvenance:
    return StageInferenceProvenance(
        operation=f"codex_timeline_{stage}",
        request_hash=f"{stage}-request",
        artifact_hash=f"{stage}-artifact",
    )


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
            TimelinePublicationJournal,
            Conversation,
            AudioEvidenceSpan,
            DeviceInputJob,
            TimelineReconciliationRequest,
        ],
    )
    for model in (
        DirtyEvidenceRange,
        TimelineEpisode,
        TimelineDay,
        TimelinePublicationJournal,
        Conversation,
        AudioEvidenceSpan,
        DeviceInputJob,
        TimelineReconciliationRequest,
    ):
        await model.delete_all()
    # The user row is not part of this suite; UTC is the documented fallback.
    monkeypatch.setattr(reconciliation, "_user_timezone", _always_utc)

    @asynccontextmanager
    async def unlocked(*_args, **_kwargs):
        yield

    monkeypatch.setattr(publication, "distributed_lock", unlocked)
    monkeypatch.setattr(dirty_ranges, "distributed_lock", unlocked)
    counter = _RedisCounterFake()
    monkeypatch.setattr(dirty_ranges, "create_async_redis", lambda **_: counter)
    yield database
    await client.drop_database("test_reconciliation_db")
    client.close()


async def _always_utc(_user_id: str) -> str:
    return "UTC"


class _RedisCounterFake:
    def __init__(self):
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str):
        return self.values.get(key)

    async def aclose(self) -> None:
        return None


# ── Builders ─────────────────────────────────────────────────────────────────


def evidence_item(offset_minutes: float, minutes: float, index: int = 1):
    start = START + timedelta(minutes=offset_minutes)
    evidence_id = f"transcript:{index}"
    return TimelineEvidenceItem(
        evidence_id=evidence_id,
        kind="transcript",
        locator=EvidenceLocator(
            capture_source_id="test-transcript", modality="transcript", track_id="mic"
        ),
        started_at=start,
        ended_at=start + timedelta(minutes=minutes),
        role="user_statement",
        excerpt="hello",
        anchor_ids=[f"{evidence_id}:start", f"{evidence_id}:end"],
    )


def make_bundle(
    *,
    bounds=(0, 60),
    items=None,
    existing=None,
    pinned=None,
    evidence_revision=1,
    manifest_hash="hash-1",
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
        evidence_revision=manifest_hash,
        windows=[
            TimelineCoverageWindow(
                window_id="w1",
                started_at=started_at,
                ended_at=ended_at,
                evidence_ids=[item.evidence_id for item in items],
            )
        ],
        evidence=items,
        anchors=[
            EvidenceAnchor(
                anchor_id=anchor_id,
                evidence_id=item.evidence_id,
                locator=item.locator,
                support_type="transcript_edge",
                earliest_at=at,
                latest_at=at,
                source_position=edge,
            )
            for item in items
            if item.anchor_ids
            for anchor_id, at, edge in (
                (item.anchor_ids[0], item.started_at, "start"),
                (item.anchor_ids[-1], item.ended_at or item.started_at, "end"),
            )
        ],
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


def publish_of(
    *episodes: AgentEpisode,
    predecessors: list[TimelineEpisode] | None = None,
    lineage_action: str | None = None,
) -> Publish:
    predecessors = predecessors or []
    if lineage_action is None:
        lineage_action = (
            "carry" if len(predecessors) == 1 and len(episodes) == 1 else "new"
        )
    refs = [
        EpisodeRevisionRef(episode_key=item.episode_key, revision=item.revision)
        for item in predecessors
    ]
    hypotheses = []
    interpreted = []
    for index, episode in enumerate(episodes):
        hypothesis_id = f"hypothesis-{index}"
        hypotheses.append(
            SeparatedEpisode(
                hypothesis_id=hypothesis_id,
                started_at=episode.started_at,
                ended_at=episode.ended_at,
                evidence_ids=list(episode.evidence_ids),
                start_anchor_ids=[f"{episode.evidence_ids[0]}:start"],
                end_anchor_ids=[f"{episode.evidence_ids[-1]}:end"],
                lineage=EpisodeLineageProposal(
                    action=lineage_action, predecessor_revisions=refs
                ),
                confidence=episode.confidence,
            )
        )
        interpreted.append(
            InterpretedEpisode(
                hypothesis_id=hypothesis_id,
                **episode.model_dump(
                    exclude={"started_at", "ended_at", "evidence_ids"}
                ),
            )
        )
    return Publish(
        projection=ValidatedTimelineProjection(episodes=list(episodes)),
        separation=SeparationResult(hypotheses=hypotheses),
        interpretation=InterpretationResult(accepted=interpreted),
        separation_inference=_stage_provenance("separation"),
        interpretation_inference=_stage_provenance("interpretation"),
    )


def publish_with_rejection(
    accepted_episode: AgentEpisode | None,
    rejected_episode: AgentEpisode,
) -> Publish:
    hypotheses = []
    accepted = []
    episodes = []
    if accepted_episode is not None:
        episodes.append(accepted_episode)
        hypotheses.append(
            SeparatedEpisode(
                hypothesis_id="hypothesis-accepted",
                started_at=accepted_episode.started_at,
                ended_at=accepted_episode.ended_at,
                evidence_ids=list(accepted_episode.evidence_ids),
                start_anchor_ids=[f"{accepted_episode.evidence_ids[0]}:start"],
                end_anchor_ids=[f"{accepted_episode.evidence_ids[-1]}:end"],
                lineage=EpisodeLineageProposal(action="new"),
                confidence=accepted_episode.confidence,
            )
        )
        accepted.append(
            InterpretedEpisode(
                hypothesis_id="hypothesis-accepted",
                **accepted_episode.model_dump(
                    exclude={"started_at", "ended_at", "evidence_ids"}
                ),
            )
        )
    hypotheses.append(
        SeparatedEpisode(
            hypothesis_id="hypothesis-rejected",
            started_at=rejected_episode.started_at,
            ended_at=rejected_episode.ended_at,
            evidence_ids=list(rejected_episode.evidence_ids),
            start_anchor_ids=[f"{rejected_episode.evidence_ids[0]}:start"],
            end_anchor_ids=[f"{rejected_episode.evidence_ids[-1]}:end"],
            lineage=EpisodeLineageProposal(action="new"),
            confidence=rejected_episode.confidence,
        )
    )
    return Publish(
        projection=ValidatedTimelineProjection(episodes=episodes),
        separation=SeparationResult(hypotheses=hypotheses),
        interpretation=InterpretationResult(
            accepted=accepted,
            rejected=[
                RejectedHypothesis(
                    hypothesis_id="hypothesis-rejected",
                    reason_code="mixed_activities",
                    explanation="The assigned evidence contains two activities.",
                    implicated_evidence_ids=list(rejected_episode.evidence_ids),
                )
            ],
        ),
        separation_inference=_stage_provenance("separation"),
        interpretation_inference=_stage_provenance("interpretation"),
    )


def publish_with_rejections(
    accepted_episode: AgentEpisode | None,
    rejected_episodes: list[AgentEpisode],
) -> Publish:
    action = publish_with_rejection(accepted_episode, rejected_episodes[0])
    hypotheses = list(action.separation.hypotheses)
    rejected = list(action.interpretation.rejected)
    for index, episode in enumerate(rejected_episodes[1:], start=2):
        hypothesis_id = f"hypothesis-rejected-{index}"
        hypotheses.append(
            SeparatedEpisode(
                hypothesis_id=hypothesis_id,
                started_at=episode.started_at,
                ended_at=episode.ended_at,
                evidence_ids=list(episode.evidence_ids),
                start_anchor_ids=[f"{episode.evidence_ids[0]}:start"],
                end_anchor_ids=[f"{episode.evidence_ids[-1]}:end"],
                lineage=EpisodeLineageProposal(action="new"),
                confidence=episode.confidence,
            )
        )
        rejected.append(
            RejectedHypothesis(
                hypothesis_id=hypothesis_id,
                reason_code="mixed_activities",
                explanation="The assigned evidence contains two activities.",
                implicated_evidence_ids=list(episode.evidence_ids),
            )
        )
    return action.model_copy(
        update={
            "separation": action.separation.model_copy(
                update={"hypotheses": hypotheses}
            ),
            "interpretation": action.interpretation.model_copy(
                update={"rejected": rejected}
            ),
        }
    )


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
    started_at = START + timedelta(minutes=offset_minutes)
    ended_at = START + timedelta(minutes=offset_minutes + minutes)
    row = DirtyEvidenceRange(
        user_id=USER,
        started_at=started_at,
        ended_at=ended_at,
        authorized_started_at=started_at,
        authorized_ended_at=ended_at,
        evidence_revision=revision,
        leased_evidence_revision=revision,
        not_before=START,
        force_after=START,
        state="leased",
        lease_owner="test-worker",
        attempts=1,
        dispatch_authorized_at=START,
        reconciliation_request_id=f"request-{offset_minutes}-{minutes}-{revision}",
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


# ── Typed context acquisition ────────────────────────────────────────────────


def context_action(
    dirty: DirtyEvidenceRange,
    bundle: EvidenceBundle,
    *,
    manifest_hash: str | None = None,
) -> RequestMoreContext:
    return RequestMoreContext(
        request=StageContextRequest(
            context_request_id="context-one",
            hypothesis_id="hypothesis-one",
            stage="separation",
            locator=EvidenceLocator(
                capture_source_id="screenpipe-one",
                modality="screen",
                track_id="display-one",
            ),
            started_at=START - timedelta(minutes=2),
            ended_at=START + timedelta(minutes=12),
            base_manifest_hash=manifest_hash or bundle.manifest.evidence_revision,
            leased_evidence_revision=dirty.leased_evidence_revision,
            target_resolution="one_frame_per_10_seconds",
            max_items=12,
            reason="ambiguous screen transition",
        )
    )


@pytest.mark.asyncio
async def test_typed_context_request_parks_and_enqueues_exact_job(documents):
    dirty = await make_dirty_range()
    bundle = make_bundle()
    action = context_action(dirty, bundle)
    expected_request = dirty_ranges.bind_context_request(dirty, action.request)
    executor = ScriptedExecutor(action)
    seen: list[tuple[datetime, datetime]] = []

    outcome = await reconcile_range(
        dirty,
        executor=executor,
        load_evidence=evidence_loader(bundle, seen),
    )

    assert outcome is None
    assert len(executor.calls) == 1
    assert len(seen) == 1
    refreshed = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    job = await DeviceInputJob.find_one(
        DeviceInputJob.context_request_id == expected_request.context_request_id
    )
    assert refreshed.state == "awaiting_context"
    assert refreshed.lease_owner is None
    assert refreshed.base_manifest_hash == bundle.manifest.evidence_revision
    assert refreshed.context_requests[0].device_input_job_ids == [str(job.id)]
    assert _utc(job.start_at) == START - timedelta(minutes=2)
    assert _utc(job.end_at) == START + timedelta(minutes=12)
    assert job.idempotency_key


@pytest.mark.asyncio
async def test_context_request_with_stale_manifest_fence_is_rejected(documents):
    dirty = await make_dirty_range()
    bundle = make_bundle()
    executor = ScriptedExecutor(
        context_action(dirty, bundle, manifest_hash="stale-manifest")
    )

    with pytest.raises(ValueError, match="bounded manifest"):
        await reconcile_range(
            dirty,
            executor=executor,
            load_evidence=evidence_loader(bundle),
        )

    refreshed = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert refreshed.state == "leased"
    assert await DeviceInputJob.find_all().count() == 0


@pytest.mark.asyncio
async def test_wait_for_future_evidence_parks_without_publishing(documents):
    dirty = await make_dirty_range()
    executor = ScriptedExecutor(WaitForFutureEvidence(reason="recording still open"))

    outcome = await reconcile_range(
        dirty,
        executor=executor,
        load_evidence=evidence_loader(make_bundle()),
    )

    assert outcome is None
    assert await TimelineEpisode.find_all().count() == 0
    refreshed = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert refreshed.state == "waiting"
    assert "waiting:recording still open" in refreshed.trigger_reasons


@pytest.mark.asyncio
async def test_publish_rebuilds_and_fences_on_a_changed_bounded_manifest(documents):
    dirty = await make_dirty_range()
    first = make_bundle(manifest_hash="hash-before")
    changed = make_bundle(manifest_hash="hash-after")
    bundles = iter([first, changed])

    async def changing_loader(*_args, **_kwargs):
        return next(bundles)

    outcome = await reconcile_range(
        dirty,
        executor=ScriptedExecutor(publish_of(agent_episode(10, 10))),
        load_evidence=changing_loader,
    )

    assert outcome == PublishResult(fenced=False)
    assert await TimelineEpisode.find_all().count() == 0


@pytest.mark.asyncio
async def test_new_episode_may_overlap_a_field_pinned_episode(documents):
    """A pin owns named fields; it does not reserve an interval."""

    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=15, minutes=30, title="Standup")
    prior.confirmed_fields = ["title"]
    await prior.save()
    bundle = make_bundle(
        existing=[existing_payload(prior)], pinned=[existing_payload(prior)]
    )

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        publish_of(agent_episode(10, 20, title="Independent work")),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
    )

    assert outcome.fenced and outcome.material_change
    active = await TimelineEpisode.find({"status": {"$ne": "superseded"}}).to_list()
    assert {item.title for item in active} == {"Standup", "Independent work"}


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
async def test_measured_quiet_settles_despite_continuing_screen_evidence(
    documents, monkeypatch
):
    """Continuous ScreenPipe frames are not proof that people kept speaking."""

    bundle = make_bundle(
        items=[
            evidence_item(10, 10),
            TimelineEvidenceItem(
                evidence_id="frame:2",
                kind="frame",
                locator=EvidenceLocator(
                    capture_source_id="screenpipe-test",
                    modality="photo",
                    track_id="display-1",
                ),
                started_at=START + timedelta(minutes=22),
                ended_at=START + timedelta(minutes=23),
                role="ambient",
            ),
            TimelineEvidenceItem(
                evidence_id="frame:3",
                kind="frame",
                locator=EvidenceLocator(
                    capture_source_id="screenpipe-test",
                    modality="photo",
                    track_id="display-1",
                ),
                started_at=START + timedelta(minutes=40),
                ended_at=START + timedelta(minutes=41),
                role="ambient",
            ),
        ]
    )

    async def measured_quiet(_end, _window):
        return True

    monkeypatch.setattr(reconciliation, "_boundary_is_quiet", measured_quiet)

    status = await assess_settlement(
        agent_episode(10, 10), bundle, START + timedelta(minutes=60), user_id=USER
    )

    assert status == "settled"


@pytest.mark.asyncio
async def test_unscored_audio_span_keeps_the_episode_provisional(documents):
    await AudioEvidenceSpan(
        user_id=USER,
        source_id="screenpipe",
        locator=EvidenceLocator(
            capture_source_id="screenpipe", modality="audio", track_id="input"
        ),
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
async def test_published_lineage_includes_conversation_from_cited_evidence(documents):
    dirty = await make_dirty_range()
    item = evidence_item(10, 10)
    item.metadata["conversation_id"] = "screenpipe-conversation"

    outcome = await publish_reconciliation(
        USER,
        dirty,
        make_bundle(items=[item]),
        publish_of(agent_episode(10, 10)),
        timezone_name="UTC",
    )

    assert outcome.fenced is True
    published = await TimelineEpisode.find_one({"status": {"$ne": "superseded"}})
    assert published is not None
    assert published.related_conversation_ids == ["screenpipe-conversation"]
    assert published.separation_inference_operation == "codex_timeline_separation"
    assert published.separation_request_hash == "separation-request"
    assert published.separation_artifact_hash == "separation-artifact"
    assert published.interpretation_inference_operation == (
        "codex_timeline_interpretation"
    )
    assert published.interpretation_request_hash == "interpretation-request"
    assert published.interpretation_artifact_hash == "interpretation-artifact"
    assert published.separation_result_hash not in {
        None,
        published.separation_artifact_hash,
    }
    assert published.interpretation_result_hash not in {
        None,
        published.interpretation_artifact_hash,
    }
    assert (
        published.evidence_refs[0].start_boundary_support[0].separation_artifact_hash
        == "separation-artifact"
    )
    persisted_range = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert persisted_range is not None
    assert persisted_range.separation_request_hash == "separation-request"
    assert persisted_range.separation_artifact_hash == "separation-artifact"
    assert persisted_range.interpretation_request_hash == "interpretation-request"
    assert persisted_range.interpretation_artifact_hash == "interpretation-artifact"
    day = await TimelineDay.find_one(TimelineDay.user_id == USER)
    assert day is not None and day.current_snapshot is not None
    assert (
        day.current_snapshot.episode_revisions[0].episode_key == published.episode_key
    )
    assert day.current_snapshot.episode_revisions[0].revision == published.revision


@pytest.mark.asyncio
async def test_redundant_interpretation_publishes_without_creating_pending_retry(
    documents,
):
    dirty = await make_dirty_range(minutes=50)
    bundle = make_bundle(items=[evidence_item(10, 10, index=1)])
    activity = agent_episode(
        10, 10, title="Coherent session", evidence_ids=("transcript:1",)
    )
    action = publish_with_rejection(activity, activity)
    action.interpretation.rejected[0].reason_code = "redundant_activity"
    outcome = await publish_reconciliation(
        USER, dirty, bundle, action, timezone_name="UTC"
    )
    assert outcome.fenced and len(outcome.episode_ids) == 1
    assert (
        await DirtyEvidenceRange.find_one(
            DirtyEvidenceRange.parent_dirty_range_id == dirty.dirty_range_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_interpretation_rejection_publishes_sibling_and_journals_retry(
    documents,
):
    dirty = await make_dirty_range(minutes=50)
    accepted_item = evidence_item(10, 10, index=1)
    rejected_item = evidence_item(30, 10, index=2)
    bundle = make_bundle(items=[accepted_item, rejected_item])
    action = publish_with_rejection(
        agent_episode(10, 10, title="Accepted sibling", evidence_ids=("transcript:1",)),
        agent_episode(30, 10, title="Mixed claim", evidence_ids=("transcript:2",)),
    )

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        action,
        timezone_name="UTC",
    )

    assert outcome.fenced and outcome.material_change
    published = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == outcome.episode_ids[0]
    )
    assert published.title == "Accepted sibling"
    successor = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.parent_dirty_range_id == dirty.dirty_range_id
    )
    assert successor is not None
    assert successor.state == "authorized_pending"
    assert successor.rejection_retry_depth == 1
    assert successor.rejection_hypothesis_id == "hypothesis-rejected"
    assert successor.rejection_reason_code == "mixed_activities"
    assert successor.rejection_evidence_ids == ["transcript:2"]
    assert _utc(successor.started_at) == START + timedelta(minutes=30)
    assert _utc(successor.ended_at) == START + timedelta(minutes=40)
    refreshed_parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert refreshed_parent.interpretation_rejections[0].successor_dirty_range_id == (
        successor.dirty_range_id
    )
    journal = await TimelinePublicationJournal.find_one()
    assert [operation.kind for operation in journal.operations][-1] == (
        "upsert_rejected_reconciliation_retry"
    )


@pytest.mark.asyncio
async def test_exhausted_interpretation_rejection_remains_failed_and_visible(documents):
    dirty = await make_dirty_range(offset_minutes=20, minutes=30)
    dirty.rejection_retry_depth = 2
    await dirty.save()
    rejected_item = evidence_item(30, 10, index=2)
    action = publish_with_rejection(
        None,
        agent_episode(30, 10, title="Still mixed", evidence_ids=("transcript:2",)),
    )

    outcome = await publish_reconciliation(
        USER,
        dirty,
        make_bundle(bounds=(20, 50), items=[rejected_item]),
        action,
        timezone_name="UTC",
    )

    assert outcome.fenced and not outcome.material_change
    successor = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.parent_dirty_range_id == dirty.dirty_range_id
    )
    assert successor is not None
    assert successor.state == "failed"
    assert successor.rejection_retry_depth == 3
    assert "exhausted 2 interpretation rejection retries" in successor.last_error
    parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert parent.interpretation_rejections[0].status == "exhausted"
    assert await TimelineEpisode.find_all().count() == 0


@pytest.mark.asyncio
async def test_all_rejected_hypotheses_publish_operation_only_journal_and_provenance(
    documents,
):
    dirty = await make_dirty_range(minutes=50)
    first_item = evidence_item(10, 10, index=1)
    second_item = evidence_item(30, 10, index=2)
    action = publish_with_rejections(
        None,
        [
            agent_episode(10, 10, title="Mixed one", evidence_ids=("transcript:1",)),
            agent_episode(30, 10, title="Mixed two", evidence_ids=("transcript:2",)),
        ],
    )

    outcome = await publish_reconciliation(
        USER,
        dirty,
        make_bundle(items=[first_item, second_item]),
        action,
        timezone_name="UTC",
    )

    assert outcome.fenced and not outcome.material_change
    journal = await TimelinePublicationJournal.find_one()
    assert journal is not None
    assert journal.status == "committed"
    assert journal.affected_days == []
    assert [operation.kind for operation in journal.operations] == [
        "upsert_rejected_reconciliation_retry",
        "upsert_rejected_reconciliation_retry",
    ]
    assert [operation.state for operation in journal.operations] == [
        "applied",
        "applied",
    ]
    parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert parent is not None
    assert parent.separation_request_hash == "separation-request"
    assert parent.separation_artifact_hash == "separation-artifact"
    assert parent.interpretation_request_hash == "interpretation-request"
    assert parent.interpretation_artifact_hash == "interpretation-artifact"
    assert parent.separation_result_hash
    assert parent.interpretation_result_hash
    assert len(parent.interpretation_rejections) == 2
    successors = await DirtyEvidenceRange.find(
        DirtyEvidenceRange.parent_dirty_range_id == dirty.dirty_range_id
    ).to_list()
    assert {item.rejection_hypothesis_id for item in successors} == {
        "hypothesis-rejected",
        "hypothesis-rejected-2",
    }
    assert await TimelineEpisode.find_all().count() == 0


@pytest.mark.asyncio
async def test_all_rejected_hypotheses_recover_after_first_operation_crash(
    documents,
    monkeypatch,
):
    dirty = await make_dirty_range(minutes=50)
    first_item = evidence_item(10, 10, index=1)
    second_item = evidence_item(30, 10, index=2)
    action = publish_with_rejections(
        None,
        [
            agent_episode(10, 10, title="Mixed one", evidence_ids=("transcript:1",)),
            agent_episode(30, 10, title="Mixed two", evidence_ids=("transcript:2",)),
        ],
    )
    real_publish = publication.publish_timeline_revision
    applied = 0

    async def publish_then_crash(**kwargs):
        real_apply = kwargs["apply_operation"]

        async def crash_after_first_operation(operation):
            nonlocal applied
            outcome = await real_apply(operation)
            applied += 1
            if applied == 1:
                raise RuntimeError("injected crash after rejected retry mutation")
            return outcome

        return await real_publish(
            **(kwargs | {"apply_operation": crash_after_first_operation})
        )

    monkeypatch.setattr(reconciliation, "publish_timeline_revision", publish_then_crash)
    with pytest.raises(
        publication.IncompletePublication,
        match="remains recoverable",
    ):
        await publish_reconciliation(
            USER,
            dirty,
            make_bundle(items=[first_item, second_item]),
            action,
            timezone_name="UTC",
        )

    parent_after_crash = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert parent_after_crash is not None
    assert parent_after_crash.separation_artifact_hash == "separation-artifact"
    assert parent_after_crash.interpretation_artifact_hash == "interpretation-artifact"
    assert len(parent_after_crash.interpretation_rejections) == 1
    journal = await TimelinePublicationJournal.find_one()
    assert journal is not None and journal.status == "applying"
    assert journal.affected_days == []

    monkeypatch.setattr(reconciliation, "publish_timeline_revision", real_publish)
    report = await publication.recover_timeline_publications(user_id=USER)

    assert report.committed_publication_ids == [journal.publication_id]
    recovered_parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert recovered_parent is not None
    assert len(recovered_parent.interpretation_rejections) == 2
    successors = await DirtyEvidenceRange.find(
        DirtyEvidenceRange.parent_dirty_range_id == dirty.dirty_range_id
    ).to_list()
    assert len(successors) == 2
    assert {item.rejection_hypothesis_id for item in successors} == {
        "hypothesis-rejected",
        "hypothesis-rejected-2",
    }
    assert await TimelineEpisode.find_all().count() == 0


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
        publish_of(agent_episode(10, 10, title="Revised"), predecessors=[prior]),
        observed=snapshot,
        timezone_name="UTC",
    )

    assert outcome.fenced is False
    assert outcome.material_change is False
    assert await TimelineEpisode.find_all().count() == 1, "no new row was written"
    assert projection.calls == []


@pytest.mark.asyncio
async def test_newer_overlapping_evidence_fences_only_the_older_range(
    documents,
):
    older = await make_dirty_range(revision=7)
    newer = await make_dirty_range(revision=8)
    stale_bundle = make_bundle(existing=[])

    first = await publish_reconciliation(
        USER,
        older,
        stale_bundle,
        publish_of(agent_episode(10, 10, title="Meeting")),
        observed={},
        timezone_name="UTC",
    )
    second = await publish_reconciliation(
        USER,
        newer,
        stale_bundle,
        publish_of(agent_episode(10, 10, title="Meeting")),
        observed={},
        timezone_name="UTC",
    )

    active = await TimelineEpisode.find(
        TimelineEpisode.status != "superseded"
    ).to_list()
    assert len(active) == 1
    assert first.fenced is False
    assert first.episode_ids == []
    assert active[0].episode_id == second.episode_ids[0]
    assert second.material_change is True


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
        publish_of(agent_episode(10, 10), predecessors=[prior]),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
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
async def test_unchanged_episode_can_advance_to_settled_without_a_new_revision(
    documents,
):
    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=10, minutes=10)
    bundle = make_bundle(
        items=[evidence_item(10, 10), evidence_item(40, 10, index=2)],
        existing=[existing_payload(prior)],
    )
    projection = RecordingProjection()
    dispatched: list[tuple[str, list[str]]] = []

    async def dispatch(user_id, episode_ids):
        dispatched.append((user_id, list(episode_ids)))

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        publish_of(agent_episode(10, 10), predecessors=[prior]),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
        dispatch_fn=dispatch,
        now=START + timedelta(minutes=60),
    )

    survivor = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == prior.episode_id
    )
    assert (survivor.status, survivor.revision) == ("settled", 1)
    assert outcome.material_change is False
    assert projection.calls == []
    assert dispatched == [(USER, [prior.episode_id])]


@pytest.mark.asyncio
async def test_status_only_completion_dispatch_stays_inside_evidence_boundary(
    documents, monkeypatch
):
    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=10, minutes=10)
    bundle = make_bundle(
        items=[evidence_item(10, 10), evidence_item(40, 10, index=2)],
        existing=[existing_payload(prior)],
    )
    ordering: list[str] = []

    async def dispatch(_user_id, _episode_ids):
        ordering.append("completion_dispatch")

    real_guarded_action = reconciliation.run_guarded_publication_action

    async def guarded_action_then_receive_evidence(*args, **kwargs):
        result = await real_guarded_action(*args, **kwargs)
        counter = dirty_ranges.create_async_redis()
        counter.values[timeline_evidence_revision(USER)] = dirty.evidence_revision
        await dirty_ranges.mark_evidence_dirty(
            USER,
            dirty.started_at,
            dirty.ended_at,
            "new-transcript-revision",
            "transcript_revision",
            source_kind="transcript",
        )
        ordering.append("new_evidence")
        return result

    monkeypatch.setattr(
        reconciliation,
        "run_guarded_publication_action",
        guarded_action_then_receive_evidence,
    )

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        publish_of(agent_episode(10, 10), predecessors=[prior]),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
        dispatch_fn=dispatch,
        now=START + timedelta(minutes=60),
    )

    assert outcome.fenced is True
    assert outcome.material_change is False
    assert ordering == ["completion_dispatch", "new_evidence"]


@pytest.mark.asyncio
async def test_status_only_settlement_reopens_snapshot_owner_before_dispatch(
    documents,
):
    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=10, minutes=10)
    snapshot = build_day_snapshot(
        user_id=USER,
        local_date=prior.local_date,
        timezone_name=prior.timezone,
        evidence_state_hash="e" * 64,
        episode_revisions=[
            SnapshotEpisodeRevisionRef(
                episode_key=prior.episode_key,
                revision=prior.revision,
            )
        ],
    )
    await TimelineDay(
        user_id=USER,
        local_date=prior.local_date,
        timezone=prior.timezone,
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
        snapshot_state="ready",
    ).insert()
    plan = TimelinePublicationDayPlan(
        local_date=prior.local_date,
        timezone=prior.timezone,
        base_snapshot_id=None,
        resulting_snapshot=snapshot,
    )
    publication_id, intent_hash = publication.publication_identity(
        user_id=USER,
        operation_source="projection",
        affected_days=[plan],
        operations=[],
    )
    owner = TimelinePublicationJournal(
        publication_id=publication_id,
        intent_hash=intent_hash,
        user_id=USER,
        operation_source="projection",
        affected_days=[plan],
        status="committed",
        committed_at=START,
        dispatch_pending=False,
        dispatch_completed_at=START,
    )
    await owner.insert()
    bundle = make_bundle(
        items=[evidence_item(10, 10), evidence_item(40, 10, index=2)],
        existing=[existing_payload(prior)],
    )

    async def crash_during_dispatch(_user_id, _episode_ids):
        reopened = await TimelinePublicationJournal.get(owner.id)
        assert reopened is not None
        assert reopened.dispatch_pending is True
        assert reopened.dispatch_completed_at is None
        raise RuntimeError("process stopped before completion dispatch")

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        publish_of(agent_episode(10, 10), predecessors=[prior]),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
        dispatch_fn=crash_during_dispatch,
        now=START + timedelta(minutes=60),
    )

    survivor = await TimelineEpisode.get(prior.id)
    reopened = await TimelinePublicationJournal.get(owner.id)
    assert outcome.material_change is False
    assert survivor is not None and survivor.status == "settled"
    assert reopened is not None and reopened.dispatch_pending is True
    assert reopened.dispatch_completed_at is None


@pytest.mark.asyncio
async def test_material_publication_cannot_apply_or_dispatch_stale_status_updates(
    documents, monkeypatch
):
    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=10, minutes=10)
    carried = agent_episode(10, 10)
    added = agent_episode(30, 10, title="Later chat", evidence_ids=("transcript:2",))
    bundle = make_bundle(
        items=[evidence_item(10, 10), evidence_item(30, 10, index=2)],
        existing=[existing_payload(prior)],
    )
    action = publish_of(carried, added)
    action.separation.hypotheses[0].lineage = EpisodeLineageProposal(
        action="carry",
        predecessor_revisions=[
            EpisodeRevisionRef(episode_key=prior.episode_key, revision=prior.revision)
        ],
    )
    dispatched: list[tuple[str, list[str]]] = []

    async def dispatch(user_id, episode_ids):
        dispatched.append((user_id, list(episode_ids)))

    real_publish = reconciliation.publish_timeline_revision

    async def publish_then_receive_new_evidence(**kwargs):
        journal = await real_publish(**kwargs)
        counter = dirty_ranges.create_async_redis()
        counter.values[timeline_evidence_revision(USER)] = dirty.evidence_revision
        await dirty_ranges.mark_evidence_dirty(
            USER,
            dirty.started_at,
            dirty.ended_at,
            "new-transcript-revision",
            "transcript_revision",
            source_kind="transcript",
        )
        return journal

    monkeypatch.setattr(
        reconciliation, "publish_timeline_revision", publish_then_receive_new_evidence
    )

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        action,
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
        dispatch_fn=dispatch,
        now=START + timedelta(minutes=60),
    )

    survivor = await TimelineEpisode.get(prior.id)
    assert outcome.fenced is True
    assert outcome.material_change is True
    assert survivor.status == "provisional"
    assert dispatched == []
    assert await TimelineEpisode.find_all().count() == 2


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
        publish_of(agent_episode(10, 12, title="Longer chat"), predecessors=[prior]),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
    )

    assert outcome.fenced and outcome.material_change
    assert outcome.episode_keys == [prior.episode_key]
    published = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == outcome.episode_ids[0]
    )
    assert published.revision == 2
    assert published.evidence_revision == dirty.leased_evidence_revision
    superseded = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == prior.episode_id
    )
    assert superseded.status == "superseded"
    # A same-key revision resolves by highest revision, so it needs no pointer.
    assert superseded.successor_keys == []
    assert projection.calls == []
    day = await TimelineDay.find_one(TimelineDay.local_date == date(2026, 8, 15))
    assert day is not None and day.current_snapshot_id


@pytest.mark.asyncio
async def test_revising_group_member_removes_stale_group_from_successor_snapshot(
    documents,
):
    dirty = await make_dirty_range()
    prior = await make_prior_episode(offset_minutes=10, minutes=10)
    peer = await make_prior_episode(offset_minutes=30, minutes=10, title="Peer work")
    group = TimelineSemanticGroupRevision(
        group_key="group-one",
        revision=1,
        member_revisions=[
            {"episode_key": prior.episode_key, "revision": prior.revision},
            {"episode_key": peer.episode_key, "revision": peer.revision},
        ],
        episode_ids=[prior.episode_id, peer.episode_id],
        source_snapshot_id="a" * 64,
        title="Shared work",
        summary="Two episodes represented one activity.",
        started_at=prior.started_at,
        ended_at=peer.ended_at,
    )
    base = build_day_snapshot(
        user_id=USER,
        local_date=date(2026, 8, 15),
        timezone_name="UTC",
        evidence_state_hash="e" * 64,
        episode_revisions=[
            SnapshotEpisodeRevisionRef(
                episode_key=prior.episode_key, revision=prior.revision
            ),
            SnapshotEpisodeRevisionRef(
                episode_key=peer.episode_key, revision=peer.revision
            ),
        ],
        semantic_group_revisions=[
            GroupRevisionRef(
                owner_local_date=date(2026, 8, 15),
                group_key=group.group_key,
                revision=group.revision,
            )
        ],
    )
    await TimelineDay(
        user_id=USER,
        local_date=date(2026, 8, 15),
        timezone="UTC",
        current_snapshot=base,
        current_snapshot_id=base.snapshot_id,
        snapshot_state="ready",
        semantic_group_history=[group],
    ).insert()
    bundle = make_bundle(existing=[existing_payload(prior)])

    outcome = await publish_reconciliation(
        USER,
        dirty,
        bundle,
        publish_of(agent_episode(10, 12, title="Revised"), predecessors=[prior]),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
    )

    assert outcome.fenced and outcome.material_change
    day = await TimelineDay.find_one(TimelineDay.local_date == date(2026, 8, 15))
    assert day.current_snapshot.semantic_group_revisions == []
    assert [(item.group_key, item.revision) for item in day.semantic_group_history] == [
        ("group-one", 1)
    ]


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
        publish_of(
            agent_episode(10, 10, title="First half"),
            agent_episode(25, 15, title="Second half", evidence_ids=("transcript:2",)),
            predecessors=[prior],
            lineage_action="split",
        ),
        observed=await observed_revisions(bundle),
        timezone_name="UTC",
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
        locator=EvidenceLocator(
            capture_source_id="test-transcript", modality="transcript", track_id="mic"
        ),
        started_at=midnight - timedelta(minutes=10),
        ended_at=midnight + timedelta(minutes=10),
        role="user_statement",
        anchor_ids=["transcript:1:start", "transcript:1:end"],
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
        publish_of(episode),
        observed={},
        timezone_name="UTC",
    )

    assert outcome.affected_local_dates == [date(2026, 8, 15), date(2026, 8, 16)]
    assert projection.calls == []
    days = await TimelineDay.find_all().to_list()
    assert {item.local_date for item in days} == {
        date(2026, 8, 15),
        date(2026, 8, 16),
    }
    assert all(item.current_snapshot_id for item in days)


# ── The RQ entry point ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_range_job_treats_a_dismissed_failure_as_terminal(documents):
    dirty = await make_dirty_range()
    dirty.state = "dismissed"
    dirty.last_error = "exhausted interpretation retries"
    dirty.resolution_history = [
        DirtyEvidenceRangeResolution(
            actor_user_id=USER,
            reason="Reviewed and accepted as unresolved",
        )
    ]
    await dirty.save()

    result = await timeline_jobs.reconcile_range_job.__wrapped__(dirty.dirty_range_id)

    stored = await DirtyEvidenceRange.get(dirty.id)
    assert result == {"dirty_range_id": dirty.dirty_range_id, "state": "dismissed"}
    assert stored.state == "dismissed"
    assert stored.attempts == dirty.attempts
    assert stored.last_error == "exhausted interpretation retries"


@pytest.mark.asyncio
async def test_reconcile_range_job_runs_the_registered_entry_point(
    documents, monkeypatch
):
    dirty = await make_dirty_range()
    await DirtyEvidenceRange.get_pymongo_collection().update_one(
        {"dirty_range_id": dirty.dirty_range_id},
        {
            "$set": {
                "state": "authorized_pending",
                "lease_owner": None,
                "attempts": 0,
                "dispatch_authorized_at": START,
                "reconciliation_request_id": "request-entry-point",
                "authorized_started_at": dirty.started_at,
                "authorized_ended_at": dirty.ended_at,
            }
        },
    )
    executor = ScriptedExecutor(publish_of(agent_episode(10, 10)))
    bundle = make_bundle()
    monkeypatch.setattr(
        reconciliation, "build_range_executor", lambda: executor, raising=True
    )
    monkeypatch.setattr(
        reconciliation, "load_reconciliation_evidence", evidence_loader(bundle)
    )

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


@pytest.mark.asyncio
async def test_reconcile_range_job_cannot_publish_or_complete_after_lease_recovery(
    documents, monkeypatch
):
    dirty = await make_dirty_range()
    await DirtyEvidenceRange.get_pymongo_collection().update_one(
        {"dirty_range_id": dirty.dirty_range_id},
        {
            "$set": {
                "state": "authorized_pending",
                "lease_owner": None,
                "attempts": 0,
                "dispatch_authorized_at": START,
                "reconciliation_request_id": "request-stale-entry-point",
                "authorized_started_at": dirty.started_at,
                "authorized_ended_at": dirty.ended_at,
            }
        },
    )
    bundle = make_bundle()

    class LeaseRecoveringExecutor(ScriptedExecutor):
        async def reconcile(self, evidence, *, validation_feedback=None, **kwargs):
            await DirtyEvidenceRange.get_pymongo_collection().update_one(
                {"dirty_range_id": dirty.dirty_range_id},
                {
                    "$set": {
                        "state": "authorized_pending",
                        "lease_owner": None,
                        "lease_expires_at": None,
                    }
                },
            )
            replacement = await dirty_ranges.lease_authorized_range_by_id(
                dirty.dirty_range_id, "replacement-worker"
            )
            assert replacement is not None
            return await super().reconcile(
                evidence, validation_feedback=validation_feedback, **kwargs
            )

    executor = LeaseRecoveringExecutor(publish_of(agent_episode(10, 10)))
    monkeypatch.setattr(
        reconciliation, "build_range_executor", lambda: executor, raising=True
    )
    monkeypatch.setattr(
        reconciliation, "load_reconciliation_evidence", evidence_loader(bundle)
    )

    result = await timeline_jobs.reconcile_range_job.__wrapped__(dirty.dirty_range_id)

    stored = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty.dirty_range_id
    )
    assert result["state"] == "lease_lost"
    assert stored.state == "leased"
    assert stored.lease_owner == "replacement-worker"
    assert stored.attempts == 2
    assert await TimelineEpisode.find_all().count() == 0


@pytest.mark.asyncio
async def test_publication_recovery_rejects_journal_after_newer_evidence(
    documents, monkeypatch
):
    dirty = await make_dirty_range()
    bundle = make_bundle()
    original_install = publication._install_snapshots

    async def crash_before_snapshot_install(_journal):
        raise RuntimeError("crash before snapshot install")

    monkeypatch.setattr(
        publication, "_install_snapshots", crash_before_snapshot_install
    )
    with pytest.raises(publication.IncompletePublication):
        await publish_reconciliation(
            USER,
            dirty,
            bundle,
            publish_of(agent_episode(10, 10)),
            observed={},
            timezone_name="UTC",
        )

    journal = await TimelinePublicationJournal.find_one({})
    assert journal is not None
    counter = dirty_ranges.create_async_redis()
    counter.values[timeline_evidence_revision(USER)] = dirty.evidence_revision
    await dirty_ranges.mark_evidence_dirty(
        USER,
        dirty.started_at,
        dirty.ended_at,
        "new-transcript-revision",
        "transcript_revision",
        source_kind="transcript",
    )
    monkeypatch.setattr(publication, "_install_snapshots", original_install)

    recovered = await timeline_jobs.recover_timeline_publications_job.__wrapped__()

    stored_journal = await TimelinePublicationJournal.get(journal.id)
    day = await TimelineDay.find_one({})
    assert recovered["committed_publication_ids"] == []
    assert recovered["conflicted_publication_ids"] == [journal.publication_id]
    assert stored_journal.status == "conflict"
    assert day.snapshot_state == "dirty"


@pytest.mark.asyncio
async def test_publication_recovery_renews_an_expired_exact_lease(
    documents, monkeypatch
):
    dirty = await make_dirty_range()
    bundle = make_bundle()
    original_install = publication._install_snapshots

    async def crash_before_snapshot_install(_journal):
        raise RuntimeError("crash before snapshot install")

    monkeypatch.setattr(
        publication, "_install_snapshots", crash_before_snapshot_install
    )
    with pytest.raises(publication.IncompletePublication):
        await publish_reconciliation(
            USER,
            dirty,
            bundle,
            publish_of(agent_episode(10, 10)),
            observed={},
            timezone_name="UTC",
        )

    journal = await TimelinePublicationJournal.find_one({})
    assert journal is not None and journal.evidence_fence is not None
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await DirtyEvidenceRange.get_pymongo_collection().update_one(
        {"_id": dirty.id}, {"$set": {"lease_expires_at": expired_at}}
    )
    monkeypatch.setattr(publication, "_install_snapshots", original_install)

    recovered = await timeline_jobs.recover_timeline_publications_job.__wrapped__()

    stored_journal = await TimelinePublicationJournal.get(journal.id)
    stored_range = await DirtyEvidenceRange.get(dirty.id)
    assert recovered["committed_publication_ids"] == [journal.publication_id]
    assert recovered["conflicted_publication_ids"] == []
    assert stored_journal.status == "committed"
    assert _utc(stored_range.lease_expires_at) > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_publication_recovery_rejects_a_reclaimed_same_range_lease(
    documents, monkeypatch
):
    dirty = await make_dirty_range()
    bundle = make_bundle()
    original_install = publication._install_snapshots

    async def crash_before_snapshot_install(_journal):
        raise RuntimeError("crash before snapshot install")

    monkeypatch.setattr(
        publication, "_install_snapshots", crash_before_snapshot_install
    )
    with pytest.raises(publication.IncompletePublication):
        await publish_reconciliation(
            USER,
            dirty,
            bundle,
            publish_of(agent_episode(10, 10)),
            observed={},
            timezone_name="UTC",
        )

    journal = await TimelinePublicationJournal.find_one({})
    assert journal is not None and journal.evidence_fence is not None
    await DirtyEvidenceRange.get_pymongo_collection().update_one(
        {"_id": dirty.id},
        {
            "$set": {
                "state": "authorized_pending",
                "lease_owner": None,
                "lease_expires_at": None,
            }
        },
    )
    replacement = await dirty_ranges.lease_authorized_range_by_id(
        dirty.dirty_range_id, "replacement-worker"
    )
    assert replacement is not None
    assert replacement.attempts == journal.evidence_fence.lease_attempt + 1
    monkeypatch.setattr(publication, "_install_snapshots", original_install)

    recovered = await timeline_jobs.recover_timeline_publications_job.__wrapped__()

    stored_journal = await TimelinePublicationJournal.get(journal.id)
    assert recovered["committed_publication_ids"] == []
    assert recovered["conflicted_publication_ids"] == [journal.publication_id]
    assert stored_journal.status == "conflict"
    stored_range = await DirtyEvidenceRange.get(dirty.id)
    assert stored_range.state == "leased"
    assert stored_range.lease_owner == "replacement-worker"


@pytest.mark.asyncio
async def test_successful_retry_resolves_only_fully_covered_older_failures(documents):
    older = await make_dirty_range(minutes=20, revision=5)
    wider = await make_dirty_range(minutes=90, revision=5)
    newer = await make_dirty_range(minutes=20, revision=9)
    foreign = await make_dirty_range(minutes=20, revision=5)
    foreign.user_id = "another-user"
    for row in (older, wider, newer, foreign):
        row.state = "failed"
        row.last_error = "original diagnostic"
        await row.save()
    successful = await make_dirty_range(minutes=30, revision=7)
    assert (
        await reconciliation.finish_range(successful, PublishResult(fenced=True))
        == "completed"
    )
    resolved = await DirtyEvidenceRange.get(older.id)
    assert resolved.state == "superseded"
    assert resolved.last_error == "original diagnostic"
    assert resolved.superseded_by_dirty_range_id == successful.dirty_range_id
    for row in (wider, newer, foreign):
        assert (await DirtyEvidenceRange.get(row.id)).state == "failed"


@pytest.mark.asyncio
async def test_publication_drops_capture_only_output_but_preserves_real_speech(
    documents,
):
    capture = evidence_item(10, 10)
    capture.kind = "audio_span"
    capture.excerpt = None
    capture.metadata = {"state": "no_speech", "direction": "output"}
    outcome = await publish_reconciliation(
        USER,
        await make_dirty_range(),
        make_bundle(items=[capture]),
        publish_of(agent_episode(10, 10, title="Audio output is active")),
        timezone_name="UTC",
    )
    assert outcome.fenced
    assert await TimelineEpisode.count() == 0
    outcome = await publish_reconciliation(
        USER,
        await make_dirty_range(),
        make_bundle(),
        publish_of(agent_episode(10, 10)),
        timezone_name="UTC",
    )
    assert outcome.fenced
    assert await TimelineEpisode.count() == 1


@pytest.mark.asyncio
async def test_not_activity_route_blocks_regenerated_claim_but_allows_new_evidence(
    documents,
):
    dirty = await make_dirty_range()
    await publish_reconciliation(
        USER,
        dirty,
        make_bundle(),
        publish_of(agent_episode(10, 10)),
        timezone_name="Etc/UTC",
    )
    episode = await TimelineEpisode.find_one({"user_id": USER})
    day = await TimelineDay.find_one({"user_id": USER})
    await reject_timeline_activity(
        episode.episode_id,
        NotActivityRequest(
            local_date=day.local_date,
            timezone=day.timezone,
            snapshot_id=day.current_snapshot_id,
            revision=episode.revision,
        ),
        SimpleNamespace(id=USER),
    )
    removed = await TimelineEpisode.get(episode.id)
    assert removed.status == "superseded"
    assert removed.evidence_refs == episode.evidence_refs
    day = await TimelineDay.get(day.id)
    assert day.review_decisions[-1].action == "episode_not_activity"
    # A renamed episode and new run do not bypass the recorded decision.
    outcome = await publish_reconciliation(
        USER,
        await make_dirty_range(),
        make_bundle(),
        publish_of(agent_episode(10, 10, title="Renamed activity")),
        timezone_name="Etc/UTC",
    )
    assert outcome.fenced
    assert await TimelineEpisode.find({"status": {"$ne": "superseded"}}).count() == 0
    # A different recording in the same time interval remains eligible.
    fresh = evidence_item(10, 10, index=2)
    outcome = await publish_reconciliation(
        USER,
        await make_dirty_range(),
        make_bundle(items=[fresh]),
        publish_of(agent_episode(10, 10, evidence_ids=["transcript:2"])),
        timezone_name="Etc/UTC",
    )
    assert outcome.fenced
    assert await TimelineEpisode.find({"status": {"$ne": "superseded"}}).count() == 1
