"""Crash-safe dirty-first Timeline publication journal."""

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.timeline import (
    DirtyEvidenceRange,
    EpisodeRevisionRef,
    GroupRevisionRef,
    TimelineDay,
    TimelineEpisode,
    TimelineInterpretationRejectionState,
    TimelinePublicationDayPlan,
    TimelinePublicationJournal,
    TimelineReviewDecision,
    TimelineSemanticGroupRevision,
)
from backend.services.timeline.publication import (
    IncompletePublication,
    _apply_operations,
    _default_operation_applier,
    _install_snapshots,
    _mark_days_dirty,
    _roll_forward_publication_locked,
    build_publication_operation,
    prepare_publication,
    publication_identity,
    publish_timeline_revision,
    recover_timeline_publications,
)
from backend.services.timeline.snapshots import build_day_snapshot
from backend.workers.timeline_jobs import recover_timeline_publications_job

DB_NAME = "test_timeline_publication_db"


@pytest.fixture
async def publication_db(mongo_service, redis_service):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client[DB_NAME]
    await init_beanie(
        database=database,
        document_models=[
            TimelineDay,
            TimelineEpisode,
            TimelinePublicationJournal,
            DirtyEvidenceRange,
        ],
    )
    yield
    await client.drop_database(DB_NAME)
    client.close()


@pytest.mark.asyncio
async def test_publication_journal_has_bounded_dispatch_queue_index(publication_db):
    indexes = (
        await TimelinePublicationJournal.get_pymongo_collection().index_information()
    )

    assert indexes["timeline_publication_dispatch_recovery"]["key"] == [
        ("status", 1),
        ("dispatch_pending", 1),
        ("committed_at", 1),
    ]


def _snapshot(revision: int, *, created_at: datetime | None = None):
    return build_day_snapshot(
        user_id="user-1",
        local_date=date(2026, 9, 3),
        timezone_name="UTC",
        evidence_state_hash=(str(revision) * 64)[:64],
        episode_revisions=[
            EpisodeRevisionRef(episode_key="episode-a", revision=revision)
        ],
        created_at=created_at,
    )


def _plan(base, result):
    return TimelinePublicationDayPlan(
        local_date=date(2026, 9, 3),
        timezone="UTC",
        base_snapshot_id=base.snapshot_id if base else None,
        resulting_snapshot=result,
    )


async def _day(base, **overrides):
    payload = {
        "user_id": "user-1",
        "local_date": date(2026, 9, 3),
        "timezone": "UTC",
        "current_snapshot": base,
        "current_snapshot_id": base.snapshot_id if base else None,
        "snapshot_state": "ready" if base else "dirty",
    }
    payload.update(overrides)
    day = TimelineDay(**payload)
    await day.insert()
    return day


def _recovery_matrix_intent(base):
    successor = TimelineEpisode(
        episode_id="episode-row-b",
        episode_key="episode-b",
        run_id="range-b",
        user_id="user-1",
        local_date=date(2026, 9, 3),
        timezone="UTC",
        started_at=datetime(2026, 9, 3, 9, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 3, 10, tzinfo=timezone.utc),
        kind="work",
        title="Recover publication",
        summary="Exercise every durable publication boundary.",
        status="settled",
        revision=1,
        evidence_revision=2,
        confidence=0.9,
        activity_mode="foreground",
    )
    group = TimelineSemanticGroupRevision(
        group_key="group-one",
        revision=1,
        member_revisions=[
            EpisodeRevisionRef(episode_key="episode-a", revision=1),
            EpisodeRevisionRef(episode_key="episode-b", revision=1),
        ],
        episode_ids=["episode-row-a", successor.episode_id],
        source_snapshot_id=base.snapshot_id,
        title="One recovery task",
        summary="The two episode revisions belong to one activity.",
        started_at=datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
        ended_at=successor.ended_at,
    )
    result = build_day_snapshot(
        user_id="user-1",
        local_date=date(2026, 9, 3),
        timezone_name="UTC",
        evidence_state_hash="2" * 64,
        episode_revisions=group.member_revisions,
        semantic_group_revisions=[
            GroupRevisionRef(
                owner_local_date=date(2026, 9, 3),
                group_key=group.group_key,
                revision=group.revision,
            )
        ],
    )
    operations = [
        build_publication_operation(
            sequence=0,
            kind="insert_episode_revision",
            expected_revision=0,
            payload={"episode": successor.model_dump(mode="json", exclude={"id"})},
        ),
        build_publication_operation(
            sequence=1,
            kind="insert_group_revision",
            expected_revision=0,
            payload={
                "local_date": "2026-09-03",
                "timezone": "UTC",
                "revision": group.model_dump(mode="json"),
            },
        ),
    ]
    return _plan(base, result), operations, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint",
    [
        "after_prepare",
        "after_day_dirtying",
        "after_episode_mutation",
        "after_group_mutation",
        "after_snapshot_install",
        "after_commit",
    ],
)
async def test_publication_recovery_is_idempotent_at_each_durable_boundary(
    publication_db, failpoint
):
    base = _snapshot(1)
    await _day(base)
    plan, operations, result = _recovery_matrix_intent(base)
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="agent",
        affected_days=[plan],
        operations=operations,
    )

    if failpoint == "after_day_dirtying":
        await _mark_days_dirty(journal)
    elif failpoint in {"after_episode_mutation", "after_group_mutation"}:
        await _mark_days_dirty(journal)
        target_kind = (
            "insert_episode_revision"
            if failpoint == "after_episode_mutation"
            else "insert_group_revision"
        )

        async def apply_then_crash(operation):
            outcome = await _default_operation_applier(journal, operation)
            if operation.kind == target_kind:
                raise RuntimeError(f"crash after {target_kind}")
            return outcome

        with pytest.raises(RuntimeError, match=f"crash after {target_kind}"):
            await _apply_operations(journal, apply_then_crash)
    elif failpoint == "after_snapshot_install":
        await _mark_days_dirty(journal)
        await _apply_operations(journal, None)
        await _install_snapshots(journal)
    elif failpoint == "after_commit":
        await _roll_forward_publication_locked(journal)

    report = await recover_timeline_publications()
    second_report = await recover_timeline_publications()

    stored_journal = await TimelinePublicationJournal.find_one(
        TimelinePublicationJournal.publication_id == journal.publication_id
    )
    day = await TimelineDay.find_one(TimelineDay.user_id == "user-1")
    episode_count = await TimelineEpisode.find(
        TimelineEpisode.user_id == "user-1",
        TimelineEpisode.episode_key == "episode-b",
        TimelineEpisode.revision == 1,
    ).count()
    assert stored_journal.status == "committed"
    assert [item.state for item in stored_journal.operations] == [
        "applied",
        "applied",
    ]
    assert day.current_snapshot_id == result.snapshot_id
    assert day.pending_publication_id is None
    assert episode_count == 1
    assert [(item.group_key, item.revision) for item in day.semantic_group_history] == [
        ("group-one", 1)
    ]
    assert report.committed_publication_ids == (
        [] if failpoint == "after_commit" else [journal.publication_id]
    )
    assert report.failed_publication_ids == []
    assert second_report.committed_publication_ids == []
    assert second_report.failed_publication_ids == []


def test_publication_identity_excludes_snapshot_creation_time():
    first = _snapshot(2, created_at=datetime(2026, 9, 3, tzinfo=timezone.utc))
    second = _snapshot(2, created_at=datetime(2026, 9, 4, tzinfo=timezone.utc))

    first_id = publication_identity(
        user_id="user-1",
        operation_source="agent",
        affected_days=[_plan(None, first)],
        operations=[],
    )
    second_id = publication_identity(
        user_id="user-1",
        operation_source="agent",
        affected_days=[_plan(None, second)],
        operations=[],
    )
    assert first_id == second_id


@pytest.mark.asyncio
async def test_operation_only_publication_commits_without_a_day(publication_db):
    operation = build_publication_operation(
        sequence=0,
        kind="supersede_episode_revision",
        payload={"episode_key": "operation-only"},
    )

    async def apply(_operation):
        return "applied"

    journal = await publish_timeline_revision(
        user_id="user-1",
        operation_source="agent",
        affected_days=[],
        operations=[operation],
        apply_operation=apply,
    )

    assert journal.status == "committed"
    assert journal.affected_days == []
    assert [item.state for item in journal.operations] == ["applied"]


@pytest.mark.asyncio
async def test_prepared_journal_dirties_day_applies_in_order_and_installs_snapshot(
    publication_db,
):
    base, result = _snapshot(1), _snapshot(2)
    await _day(base)
    operations = [
        build_publication_operation(
            sequence=0,
            kind="insert_episode_revision",
            expected_revision=1,
            payload={"episode_key": "episode-a", "revision": 2},
        ),
        build_publication_operation(
            sequence=1,
            kind="supersede_episode_revision",
            expected_revision=1,
            payload={"episode_key": "episode-a"},
        ),
    ]
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="agent",
        affected_days=[_plan(base, result)],
        operations=operations,
    )
    seen = []

    async def apply(operation):
        day = await TimelineDay.find_one(TimelineDay.user_id == "user-1")
        assert day.snapshot_state == "dirty"
        assert day.pending_publication_id == journal.publication_id
        seen.append(operation.kind)
        return "applied"

    await _roll_forward_publication_locked(journal, apply_operation=apply)

    stored = await TimelinePublicationJournal.find_one(
        TimelinePublicationJournal.publication_id == journal.publication_id
    )
    day = await TimelineDay.find_one(TimelineDay.user_id == "user-1")
    assert seen == ["insert_episode_revision", "supersede_episode_revision"]
    assert stored.status == "committed"
    assert [item.state for item in stored.operations] == ["applied", "applied"]
    assert day.current_snapshot_id == result.snapshot_id
    assert day.current_snapshot.snapshot_id == result.snapshot_id
    assert day.pending_publication_id is None
    assert day.snapshot_state == "ready"


@pytest.mark.asyncio
async def test_crash_after_graph_mutation_rolls_forward_without_duplicate_operation(
    publication_db,
):
    base, result = _snapshot(1), _snapshot(2)
    await _day(base)
    operation = build_publication_operation(
        sequence=0,
        kind="insert_episode_revision",
        payload={"episode_key": "episode-a", "revision": 2},
    )
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="manual",
        affected_days=[_plan(base, result)],
        operations=[operation],
    )
    landed = set()

    async def crash_after_mutation(item):
        landed.add(item.operation_id)
        raise RuntimeError("injected crash after mutation")

    with pytest.raises(IncompletePublication, match="remains recoverable"):
        await _roll_forward_publication_locked(
            journal, apply_operation=crash_after_mutation
        )
    dirty = await TimelineDay.find_one(TimelineDay.user_id == "user-1")
    assert dirty.snapshot_state == "dirty"
    assert dirty.current_snapshot_id == base.snapshot_id

    calls = 0

    async def prove_idempotent(item):
        nonlocal calls
        calls += 1
        assert item.operation_id in landed
        return "already_applied"

    fresh = await TimelinePublicationJournal.find_one(
        TimelinePublicationJournal.publication_id == journal.publication_id
    )
    await _roll_forward_publication_locked(fresh, apply_operation=prove_idempotent)

    recovered = await TimelineDay.find_one(TimelineDay.user_id == "user-1")
    assert calls == 1
    assert len(landed) == 1
    assert recovered.current_snapshot_id == result.snapshot_id
    assert recovered.snapshot_state == "ready"


@pytest.mark.asyncio
async def test_manual_review_reset_is_recovered_from_persisted_publication_intent(
    publication_db,
):
    base, result = _snapshot(1), _snapshot(2)
    reviewed_at = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    await _day(
        base,
        review_state="memory_pending",
        review_snapshot_id=base.snapshot_id,
        memory_review_proposal_id="proposal-old",
        episodes_reviewed_at=reviewed_at,
        consolidation_state="ready",
        consolidation_snapshot_id=base.snapshot_id,
        consolidation_suggestions=[{"suggestion_id": "old"}],
    )
    decision = TimelineReviewDecision(
        decision_id="manual-decision-1",
        run_id=result.snapshot_id,
        action="episode_update",
        episode_ids=["episode-row-1"],
        before={"title": "Before"},
        after={"title": "After"},
        created_at=reviewed_at,
    )
    plan = TimelinePublicationDayPlan(
        local_date=date(2026, 9, 3),
        timezone="UTC",
        base_snapshot_id=base.snapshot_id,
        resulting_snapshot=result,
        review_decision=decision,
    )
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="manual",
        affected_days=[plan],
        operations=[],
    )

    await _mark_days_dirty(journal)
    await _apply_operations(journal, None)
    await _install_snapshots(journal)
    # The process dies here, after the day write but before the journal commit.
    report = await recover_timeline_publications()

    recovered = await TimelineDay.find_one(TimelineDay.user_id == "user-1")
    assert report.committed_publication_ids == [journal.publication_id]
    assert [item.decision_id for item in recovered.review_decisions] == [
        "manual-decision-1"
    ]
    assert recovered.review_state == "episodes_pending"
    assert recovered.review_snapshot_id is None
    assert recovered.memory_review_proposal_id is None
    assert recovered.episodes_reviewed_at is None
    assert recovered.consolidation_state == ""
    assert recovered.consolidation_snapshot_id is None
    assert recovered.consolidation_suggestions == []


@pytest.mark.asyncio
async def test_snapshot_change_preserves_prior_audit_without_marking_whole_day_memory(
    publication_db,
):
    base, result = _snapshot(1), _snapshot(2)
    await _day(
        base,
        applied_snapshot_id=base.snapshot_id,
        reviewed_snapshot_id=base.snapshot_id,
        snapshot_state="applied",
    )
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="semantic_group",
        affected_days=[_plan(base, result)],
        operations=[],
    )
    await _roll_forward_publication_locked(journal)

    day = await TimelineDay.find_one(TimelineDay.user_id == "user-1")
    assert day.current_snapshot_id == result.snapshot_id
    assert day.reviewed_snapshot_id is None
    assert day.applied_snapshot_id == base.snapshot_id
    assert day.snapshot_state == "ready"


@pytest.mark.asyncio
async def test_prepare_retry_resolves_the_existing_dirty_journal(publication_db):
    base, result = _snapshot(1), _snapshot(2)
    await _day(base)
    plan = _plan(base, result)
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="projection",
        affected_days=[plan],
        operations=[],
    )

    # Use the internal stage to model a process dying after day dirtying.
    await _mark_days_dirty(journal)
    retried = await prepare_publication(
        user_id="user-1",
        operation_source="projection",
        affected_days=[plan],
        operations=[],
    )
    assert retried.publication_id == journal.publication_id
    assert retried.status == "days_dirty"


@pytest.mark.asyncio
async def test_recovery_scans_journals_and_reports_orphaned_dirty_days(publication_db):
    base, result = _snapshot(1), _snapshot(2)
    await _day(base)
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="projection",
        affected_days=[_plan(base, result)],
        operations=[],
    )
    await _mark_days_dirty(journal)
    await TimelineDay(
        user_id="user-2",
        local_date=date(2026, 9, 4),
        timezone="UTC",
        snapshot_state="dirty",
        pending_publication_id="missing-journal",
    ).insert()

    report = await recover_timeline_publications()

    assert report.committed_publication_ids == [journal.publication_id]
    assert report.failed_publication_ids == []
    assert report.orphaned_dirty_days == [("user-2", "2026-09-04", "missing-journal")]


@pytest.mark.asyncio
async def test_registered_recovery_job_replays_persisted_episode_operation(
    publication_db,
):
    base, result = _snapshot(1), _snapshot(2)
    await _day(base)
    successor = TimelineEpisode(
        episode_id="episode-row-2",
        episode_key="episode-a",
        run_id="range-2",
        user_id="user-1",
        local_date=date(2026, 9, 3),
        timezone="UTC",
        started_at=datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 3, 9, tzinfo=timezone.utc),
        kind="work",
        title="Write crash recovery",
        summary="Implemented the durable publication replay path.",
        status="settled",
        revision=2,
        evidence_revision=7,
        confidence=0.9,
        activity_mode="foreground",
    )
    operation = build_publication_operation(
        sequence=0,
        kind="insert_episode_revision",
        expected_revision=1,
        payload={"episode": successor.model_dump(mode="python")},
    )
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="agent",
        affected_days=[_plan(base, result)],
        operations=[operation],
    )
    await _mark_days_dirty(journal)

    report = await recover_timeline_publications_job.__wrapped__()

    stored = await TimelineEpisode.find_one(
        TimelineEpisode.user_id == "user-1",
        TimelineEpisode.episode_key == "episode-a",
        TimelineEpisode.revision == 2,
    )
    day = await TimelineDay.find_one(TimelineDay.user_id == "user-1")
    assert stored is not None
    assert day.current_snapshot_id == result.snapshot_id
    assert report["committed_publication_ids"] == [journal.publication_id]


@pytest.mark.asyncio
async def test_recovery_replays_rejected_hypothesis_retry_operation(publication_db):
    base, result = _snapshot(1), _snapshot(2)
    await _day(base)
    stamp = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)
    parent = DirtyEvidenceRange(
        dirty_range_id="range-parent",
        user_id="user-1",
        started_at=stamp,
        ended_at=stamp + timedelta(hours=1),
        evidence_revision=7,
        not_before=stamp,
        force_after=stamp,
        state="leased",
    )
    await parent.insert()
    successor = DirtyEvidenceRange(
        dirty_range_id="range-retry",
        user_id="user-1",
        started_at=stamp + timedelta(minutes=20),
        ended_at=stamp + timedelta(minutes=35),
        evidence_revision=7,
        not_before=stamp,
        force_after=stamp,
        state="authorized_pending",
        dispatch_authorized_at=stamp,
        reconciliation_request_id="request-1",
        authorized_started_at=stamp + timedelta(minutes=20),
        authorized_ended_at=stamp + timedelta(minutes=35),
        parent_dirty_range_id=parent.dirty_range_id,
        rejection_retry_depth=1,
        rejection_hypothesis_id="hypothesis-mixed",
        rejection_reason_code="mixed_activities",
        rejection_evidence_ids=["transcript:1"],
    )
    rejection = TimelineInterpretationRejectionState(
        hypothesis_id="hypothesis-mixed",
        reason_code="mixed_activities",
        explanation="The claim contains two activities.",
        implicated_evidence_ids=["transcript:1"],
        retry_depth=1,
        successor_dirty_range_id=successor.dirty_range_id,
        status="retry_scheduled",
        interpretation_result_hash="interpretation-hash",
        created_at=stamp,
    )
    operation = build_publication_operation(
        sequence=0,
        kind="upsert_rejected_reconciliation_retry",
        payload={
            "parent_dirty_range_id": parent.dirty_range_id,
            "rejection": rejection.model_dump(mode="python"),
            "successor": successor.model_dump(mode="python", exclude={"id"}),
        },
    )
    journal = await prepare_publication(
        user_id="user-1",
        operation_source="agent",
        affected_days=[_plan(base, result)],
        operations=[operation],
    )
    await _mark_days_dirty(journal)

    report = await recover_timeline_publications()

    stored_successor = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == successor.dirty_range_id
    )
    stored_parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == parent.dirty_range_id
    )
    assert report.committed_publication_ids == [journal.publication_id]
    assert stored_successor is not None
    assert stored_successor.state == "authorized_pending"
    assert len(stored_parent.interpretation_rejections) == 1
    stored_rejection = stored_parent.interpretation_rejections[0]
    assert stored_rejection.hypothesis_id == rejection.hypothesis_id
    assert stored_rejection.successor_dirty_range_id == successor.dirty_range_id
    assert stored_rejection.interpretation_result_hash == "interpretation-hash"
