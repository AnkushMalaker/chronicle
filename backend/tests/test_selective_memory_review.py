"""Selection, freshness and crash recovery through the production review entry points."""

import os
from contextlib import asynccontextmanager, nullcontext
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from beanie import init_beanie
from fastapi import BackgroundTasks, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.memory_audit import MemoryAuditEntry
from backend.models.timeline import (
    DirtyEvidenceRange,
    EpisodeRevisionRef,
    MemoryFreshnessResult,
    MemoryReviewProposal,
    TimelineDay,
    TimelineDaySnapshot,
    TimelineEpisode,
    TimelinePublicationJournal,
)
from backend.routers.modules import timeline_routes
from backend.services.memory.agent import review_agent
from backend.services.memory.agent.memory_agent import build_write_task
from backend.services.memory.base import DayWriteOutcome
from backend.services.memory.vault_manager import ConvDocVaultManager
from backend.services.timeline import review
from backend.services.timeline.review_storage import assert_memory_review_storage_ready
from backend.services.timeline.vault_day_index import ensure_day_episode_index


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


@pytest.fixture
async def selection_db(monkeypatch):
    client = AsyncIOMotorClient(
        os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27018"),
        serverSelectionTimeoutMS=2000,
    )
    database = client["test_selective_memory_review"]
    await init_beanie(
        database=database,
        document_models=[
            DirtyEvidenceRange,
            MemoryReviewProposal,
            TimelineEpisode,
            TimelineDay,
            TimelinePublicationJournal,
            MemoryAuditEntry,
        ],
    )
    monkeypatch.setattr(review, "distributed_lock", unlocked)
    monkeypatch.setattr(review, "vault_run_lock", lambda _: nullcontext())
    yield database
    await client.drop_database(database.name)
    client.close()


def proposal(**kwargs):
    values = dict(
        request_id="request-one",
        user_id="user-one",
        local_date=date(2026, 9, 5),
        timezone="Asia/Kolkata",
        snapshot_id="a" * 64,
        selected_episodes=[EpisodeRevisionRef(episode_key="ep-one", revision=1)],
        selected_tokens=["ep-one:1"],
        selection_hash="b" * 64,
    )
    values.update(kwargs)
    return MemoryReviewProposal(**values)


@pytest.fixture
async def vault(selection_db, tmp_path, monkeypatch):
    class Service:
        config = SimpleNamespace()

        def __init__(self, config=None):
            self.vault = ConvDocVaultManager(tmp_path)
            self.last_day_source_episode_keys_by_path = {}

        async def add_day_memory(
            self, digest, day, user, *, day_index_digest, **kwargs
        ):
            root = self.vault.user_root(user)
            ensure_day_episode_index(root / f"Daily/{day}.md", day, day_index_digest)
            return DayWriteOutcome.COMPLETE, [f"Daily/{day}.md"]

    service = Service()
    monkeypatch.setattr(review, "ChronicleMemoryService", Service)
    monkeypatch.setattr(review, "get_memory_service", lambda: service)
    root = service.vault.user_root("user-one")
    root.mkdir()
    return root


async def pending(vault, **kwargs):
    p = proposal(state="pending", **kwargs)
    before = review._snapshot(vault)
    p.vault_base_hash = review._retain_snapshot(vault, before)
    p.changes = review.build_potential_changes(
        before,
        {**before, "Topics/Plan.md": "Plan from September"},
        source_episode_keys_by_path={"Topics/Plan.md": ["ep-one"]},
    )
    await p.insert()
    return p


@pytest.mark.asyncio
async def test_new_adjacent_note_regenerates_without_applying(vault, monkeypatch):
    p = await pending(vault)
    (vault / "Topics").mkdir()
    (vault / "Topics/Existing Plan.md").write_text("Same plan under another name")
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    checker = AsyncMock(
        return_value=MemoryFreshnessResult(
            verdict="affected",
            reason="Reuse Existing Plan",
            relevant_paths=["Topics/Existing Plan.md"],
        )
    )
    monkeypatch.setattr(review, "check_freshness", checker)
    assert await review.resolve_memory_review(p, [p.changes[0].change_id]) == "checking"
    assert await review.process_memory_review_decision(p) == "regenerating"
    old = await MemoryReviewProposal.get(p.id)
    new = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.request_id == p.request_id,
        MemoryReviewProposal.generation == 2,
    )
    assert old.changes == p.changes
    assert new.proposal_id != p.proposal_id and new.requested_change_ids == []
    assert new.supersedes_proposal_id == p.proposal_id
    assert not (vault / "Topics/Plan.md").exists()
    assert "Topics/Existing Plan.md" in checker.call_args.args[2]
    with pytest.raises(review.MemoryReviewError, match="no longer pending"):
        await review.resolve_memory_review(old, [p.changes[0].change_id])


@pytest.mark.asyncio
async def test_unrelated_note_passes_check_and_exact_diff_is_applied(
    vault, monkeypatch
):
    p = await pending(vault)
    (vault / "People").mkdir()
    (vault / "People/Other.md").write_text("Unrelated")
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        review,
        "check_freshness",
        AsyncMock(
            return_value=MemoryFreshnessResult(
                verdict="unaffected", reason="Unrelated person"
            )
        ),
    )
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "applied"
    assert (vault / "Topics/Plan.md").read_text() == p.changes[0].after_text
    assert await MemoryAuditEntry.find_all().count() == 1
    assert (vault / "People/Other.md").read_text() == "Unrelated"


@pytest.mark.asyncio
async def test_failed_checker_preserves_retryable_diff(vault, monkeypatch):
    p = await pending(vault)
    (vault / "extra.md").write_text("Changed")
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        review,
        "check_freshness",
        AsyncMock(side_effect=RuntimeError("checker offline")),
    )
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "pending"
    stored = await MemoryReviewProposal.get(p.id)
    assert stored.changes == p.changes and stored.requested_change_ids == []
    assert not (vault / "Topics/Plan.md").exists()


@pytest.mark.asyncio
async def test_file_race_rechecks_before_write(vault, monkeypatch):
    p = await pending(vault)
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    apply = review._apply_review_sync
    calls = []

    def racing(p, root):
        calls.append(1)
        if len(calls) == 1:
            (root / "Other.md").write_text("arrived after check")
        return apply(p, root)

    monkeypatch.setattr(review, "_apply_review_sync", racing)
    checker = AsyncMock(
        return_value=MemoryFreshnessResult(verdict="unaffected", reason="Unrelated")
    )
    monkeypatch.setattr(review, "check_freshness", checker)
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "applied"
    assert len(calls) == 2 and checker.await_count == 2


@pytest.mark.asyncio
async def test_apply_recovers_after_audit_failure_without_duplicate_write(
    vault, monkeypatch
):
    p = await pending(vault)
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    real_audit = review.record_vault_change
    audit = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(review, "record_vault_change", audit)
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "applying"
    assert (vault / "Topics/Plan.md").exists()
    monkeypatch.setattr(review, "record_vault_change", real_audit)
    assert (await review.process_memory_review_queue())["applied"] == 1
    assert await MemoryAuditEntry.find_all().count() == 1
    assert (await review.process_memory_review_queue())["considered"] == 0


@pytest.mark.asyncio
async def test_partial_changes_do_not_mark_whole_selection_remembered(
    vault, monkeypatch
):
    p = await pending(vault)
    p.changes += review.build_potential_changes(
        {},
        {"Daily/2026-09-05.md": "daily"},
        source_episode_keys_by_path={"Daily/2026-09-05.md": ["ep-one"]},
    )
    await p.save()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    await review.process_memory_review_decision(p)
    stored = await MemoryReviewProposal.get(p.id)
    status = review.episode_review_outcomes([stored])["ep-one:1"]
    assert status["state"] == "partial" and not status["daily_recorded"]
    assert not (vault / "Daily/2026-09-05.md").exists()


def episode(key, day=5):
    return TimelineEpisode.model_construct(
        episode_id=key,
        episode_key=key,
        revision=1,
        user_id="user-one",
        run_id="run-one",
        local_date=date(2026, 9, day),
        timezone="Asia/Kolkata",
        started_at=datetime(2026, 9, day, 9, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, day, 10, tzinfo=timezone.utc),
        kind="work",
        title=key,
        summary="Bounded work summary",
        status="settled",
        confidence=0.9,
        activity_mode="foreground",
    )


@pytest.mark.asyncio
async def test_creation_duplicates_and_partial_selection_leave_siblings(
    vault, monkeypatch
):
    a, b = episode("a"), episode("b")
    for e in [a, b]:
        await e.insert()
    snapshot = TimelineDaySnapshot(
        snapshot_id="a" * 64,
        episode_revisions=[
            EpisodeRevisionRef(episode_key=e.episode_key, revision=1) for e in [a, b]
        ],
        evidence_state_hash="c" * 64,
    )
    day = TimelineDay(
        user_id="user-one",
        local_date=date(2026, 9, 5),
        timezone="Asia/Kolkata",
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
        snapshot_state="ready",
    )
    await day.insert()
    monkeypatch.setattr(
        review, "episode_revision_is_published", AsyncMock(return_value=True)
    )
    refs = [snapshot.episode_revisions[0]]
    first = await review.create_memory_selection(
        "user-one", day.local_date, day.timezone, snapshot.snapshot_id, refs
    )
    second = await review.create_memory_selection(
        "user-one", day.local_date, day.timezone, snapshot.snapshot_id, refs
    )
    assert first[0].proposal_id == second[0].proposal_id
    assert await MemoryReviewProposal.find_all().count() == 1
    assert "b:1" not in review.episode_review_outcomes(first)
    assert (await TimelineDay.get(day.id)).review_state == "episodes_pending"


@pytest.mark.asyncio
async def test_generation_fifo_not_source_date_and_pending_does_not_block(
    vault, monkeypatch
):
    one = proposal(request_id="sept", selected_tokens=["a:1"])
    two = proposal(
        request_id="jan", selected_tokens=["b:1"], local_date=date(2026, 1, 1)
    )
    await one.insert()
    await two.insert()
    monkeypatch.setattr(review, "refresh_memory_selection_states", AsyncMock())
    calls = []

    async def generate(p):
        calls.append(p.request_id)
        p.state = "pending"
        await p.save()
        return "pending"

    monkeypatch.setattr(review, "generate_memory_review", generate)
    assert (await review.process_memory_review_queue())["pending"] == 2
    assert calls == ["sept", "jan"]


@pytest.mark.asyncio
async def test_real_generation_stages_only_selected_daily_entries(vault, monkeypatch):
    e = episode("ep-one")
    p = proposal(selection_hash=review.selection_hash([e], []))
    await p.insert()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([e], [])))
    (vault / "Daily").mkdir()
    (vault / "Daily/2026-09-05.md").write_text(
        "# 2026-09-05\n\n## Episodes\n\n- 08:00–09:00 · work · routine — Earlier <!-- episode_key:earlier -->\n"
    )
    assert await review.generate_memory_review(p) == "pending"
    generated = await MemoryReviewProposal.get(p.id)
    assert len(generated.changes) == 1
    assert "episode_key:earlier" in generated.changes[0].after_text
    assert "episode_key:ep-one" in generated.changes[0].after_text
    assert "episode_key:ep-one" not in (vault / "Daily/2026-09-05.md").read_text()
    assert generated.vault_base_hash


def test_cumulative_daily_keeps_accepted_and_ignores_unselected():
    before = "# Day\n\n## Episodes\n\n- 08:00–09:00 · work <!-- episode_key:old -->\n\n## Notes\n\nHuman note\n"
    generated = (
        "# Day\n\n## Episodes\n\n- 10:00–11:00 · work <!-- episode_key:new -->\n"
    )
    result = review.cumulative_daily(before, generated, {"new"})
    assert result.index("08:00") < result.index("10:00")
    assert "Human note" in result and result.count("episode_key:old") == 1


def test_snapshot_includes_templates_and_detects_new_notes(tmp_path):
    (tmp_path / "Templates").mkdir()
    (tmp_path / "Templates/Person.md").write_text("Guidance")
    baseline = review._snapshot(tmp_path)
    assert baseline["Templates/Person.md"] == "Guidance"
    (tmp_path / "New.md").write_text("New")
    assert review._snapshot_hash(baseline) != review._snapshot_hash(
        review._snapshot(tmp_path)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", [1, 2])
async def test_every_note_boundary_recovers_from_persisted_intent(
    vault, monkeypatch, failure_at
):
    p = await pending(vault)
    p.changes += review.build_potential_changes(
        {},
        {"Topics/Second.md": "Second fact"},
        source_episode_keys_by_path={"Topics/Second.md": ["ep-one"]},
    )
    await p.save()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    write = review._atomic_write
    calls = []

    def fail(target, content):
        if target.suffix == ".md":
            calls.append(target)
            if len(calls) == failure_at:
                raise RuntimeError("interrupted note write")
        write(target, content)

    monkeypatch.setattr(review, "_atomic_write", fail)
    await review.resolve_memory_review(p, [c.change_id for c in p.changes])
    assert await review.process_memory_review_decision(p) == "applying"
    monkeypatch.setattr(review, "_atomic_write", write)
    assert (await review.process_memory_review_queue())["applied"] == 1
    assert (vault / "Topics/Second.md").read_text() == "Second fact"
    assert await MemoryAuditEntry.find_all().count() == 2


@pytest.mark.asyncio
async def test_old_source_changes_require_correction_but_sibling_snapshot_does_not(
    vault, monkeypatch
):
    e = episode("ep-one")
    p = await pending(vault, selection_hash=review.selection_hash([e], []))
    selection = AsyncMock(return_value=([e], []))
    monkeypatch.setattr(review, "_selection", selection)
    await review.refresh_memory_selection_states()
    assert (await MemoryReviewProposal.get(p.id)).state == "pending"
    e.summary = "Corrected evidence"
    await review.refresh_memory_selection_states()
    assert (await MemoryReviewProposal.get(p.id)).state == "stale"
    p = await MemoryReviewProposal.get(p.id)
    p.state = "applied"
    p.accepted_change_ids = [p.changes[0].change_id]
    await p.save()
    await review.refresh_memory_selection_states()
    assert (await MemoryReviewProposal.get(p.id)).state == "correction_required"
    assert not (
        vault / "Topics/Plan.md"
    ).exists()  # state reconciliation never writes notes


@pytest.mark.asyncio
async def test_same_key_home_date_correction_preserves_other_daily_entries(
    vault, monkeypatch
):
    old_day = "2026-09-04"
    old_note = "# Old day\n\n## Episodes\n\n- 08:00–09:00 · work <!-- episode_key:ep-one -->\n- 10:00–11:00 · work <!-- episode_key:unrelated -->\n"
    (vault / "Daily").mkdir()
    (vault / f"Daily/{old_day}.md").write_text(old_note)
    old = proposal(
        state="correction_required", active=False, local_date=date(2026, 9, 4)
    )
    old.changes = review.build_potential_changes(
        {},
        {f"Daily/{old_day}.md": old_note},
        source_episode_keys_by_path={f"Daily/{old_day}.md": ["ep-one"]},
    )
    old.accepted_change_ids = [old.changes[0].change_id]
    await old.insert()
    e = episode("ep-one")
    current = proposal(
        request_id="correction",
        correction_of=[old.proposal_id],
        correction_episode_keys=["ep-one"],
    )
    await current.insert()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([e], [])))
    assert await review.generate_memory_review(current) == "pending"
    generated = await MemoryReviewProposal.get(current.id)
    changes = {c.note_path: c for c in generated.changes}
    assert "episode_key:ep-one" not in changes[f"Daily/{old_day}.md"].after_text
    assert "episode_key:unrelated" in changes[f"Daily/{old_day}.md"].after_text
    assert "episode_key:ep-one" in changes["Daily/2026-09-05.md"].after_text
    assert (vault / f"Daily/{old_day}.md").read_text() == old_note


@pytest.mark.asyncio
async def test_readonly_checker_sees_new_names_and_fails_closed_on_incomplete_output(
    vault, monkeypatch
):
    p = await pending(vault)
    current = {"Topics/Other Name.md": "The plan already exists here"}
    calls = []

    async def assess(root, *, task, schema):
        calls.append(task)
        assert (root / "Topics/Other Name.md").exists()
        return review_agent.ReviewResult(
            reported=True,
            assessment={
                "verdict": "affected",
                "reason": "Reuse Other Name",
                "relevant_paths": ["Topics/Other Name.md"],
            },
        )

    monkeypatch.setattr(review_agent, "assess_vault_context", assess)
    result = await review.check_freshness(p, {}, current)
    assert result.verdict == "affected" and "Other Name.md" in calls[0]

    async def incomplete(*args, **kwargs):
        return review_agent.ReviewResult(
            reported=True,
            assessment={"verdict": "unaffected", "reason": "ok"},
            warnings=["truncated"],
        )

    monkeypatch.setattr(review_agent, "assess_vault_context", incomplete)
    assert (await review.check_freshness(p, {}, current)).verdict == "uncertain"


def test_temporal_task_distinguishes_capture_and_processing_and_individual_claims():
    task = build_write_task(
        "On this January recording: yesterday I left Company A. A June note says Company B.",
        "2026-01-05",
        date="2026-01-05T10:00:00+05:30",
        record="day",
    )
    assert "Processing time is not event time" in task
    assert (
        "yesterday/tomorrow relative to the timestamp of the supporting evidence"
        in task
    )
    assert "individual claims, not note last_seen" in task
    assert "2026-01-05T10:00:00+05:30" in task


@pytest.mark.asyncio
async def test_storage_guard_refuses_implicit_day_proposal_conversion(selection_db):
    await assert_memory_review_storage_ready(selection_db)
    await selection_db.memory_review_proposals.insert_one(
        {"proposal_id": "old-human-decision", "state": "applied"}
    )
    with pytest.raises(RuntimeError, match="explicit cutover"):
        await assert_memory_review_storage_ready(selection_db)
    assert (
        await selection_db.memory_review_proposals.count_documents(
            {"proposal_id": "old-human-decision"}
        )
        == 1
    )


@pytest.mark.asyncio
async def test_selection_routes_authorize_exact_revisions_and_generation(
    vault, monkeypatch
):
    a = episode("ep-one")
    await a.insert()
    refs = [EpisodeRevisionRef(episode_key=a.episode_key, revision=1)]
    snapshot = TimelineDaySnapshot(
        snapshot_id="a" * 64, episode_revisions=refs, evidence_state_hash="c" * 64
    )
    day = TimelineDay(
        user_id="user-one",
        local_date=date(2026, 9, 5),
        timezone="Asia/Kolkata",
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
        snapshot_state="ready",
    )
    await day.insert()
    monkeypatch.setattr(
        review, "episode_revision_is_published", AsyncMock(return_value=True)
    )
    tasks = BackgroundTasks()
    result = await timeline_routes.create_timeline_memory_selection(
        day.local_date,
        timeline_routes.CreateMemorySelectionRequest(
            timezone=day.timezone, snapshot_id=day.current_snapshot_id, episodes=refs
        ),
        tasks,
        SimpleNamespace(id="user-one"),
    )
    await tasks()
    row = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.proposal_id == result["proposals"][0]["proposal_id"]
    )
    assert row.state == "pending"
    with pytest.raises(HTTPException) as wrong_owner:
        await timeline_routes.resolve_timeline_memory_review(
            row.proposal_id,
            timeline_routes.ResolveMemoryReviewRequest(
                generation=1, accepted_change_ids=[]
            ),
            BackgroundTasks(),
            SimpleNamespace(id="another-user"),
        )
    assert wrong_owner.value.status_code == 404
    with pytest.raises(HTTPException) as old_generation:
        await timeline_routes.resolve_timeline_memory_review(
            row.proposal_id,
            timeline_routes.ResolveMemoryReviewRequest(
                generation=2, accepted_change_ids=[]
            ),
            BackgroundTasks(),
            SimpleNamespace(id="user-one"),
        )
    assert old_generation.value.status_code == 409
    listed = await timeline_routes.list_timeline_memory_selections(
        day.local_date, day.timezone, SimpleNamespace(id="user-one")
    )
    assert listed["outcomes"]["ep-one:1"]["state"] == "pending"
    assert listed["proposals"][0]["selected_episodes"] == [refs[0].model_dump()]


@pytest.mark.asyncio
async def test_uncommitted_successor_is_not_treated_as_withdrawn_source(
    selection_db, monkeypatch
):
    original = episode("original")
    original.status = "superseded"
    original.successor_keys = ["successor"]
    successor = episode("successor")
    await original.insert()
    await successor.insert()
    monkeypatch.setattr(
        review, "episode_revision_is_published", AsyncMock(return_value=False)
    )
    with pytest.raises(review.SelectionNotReady, match="not committed"):
        await review._current_successors(original)


@pytest.mark.asyncio
async def test_registered_queue_recovers_interrupted_generation(vault, monkeypatch):
    p = proposal(state="generating")
    await p.insert()
    await review.process_memory_review_queue()
    old = await MemoryReviewProposal.get(p.id)
    assert old.state == "stale"
    successor = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.proposal_id == old.replacement_proposal_id
    )
    assert successor.request_id == old.request_id
    assert successor.generation == 2
    assert successor.state == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["unaffected", "affected"])
async def test_partial_apply_rechecks_external_edits_and_preserves_completed_writes(
    vault, monkeypatch, verdict
):
    p = await pending(vault)
    p.changes += review.build_potential_changes(
        {},
        {"Topics/Second.md": "Second fact"},
        source_episode_keys_by_path={"Topics/Second.md": ["ep-one"]},
    )
    await p.save()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    write = review._atomic_write

    def interrupt(target, content):
        if target.name == "Second.md":
            raise RuntimeError("crash after first note")
        write(target, content)

    monkeypatch.setattr(review, "_atomic_write", interrupt)
    await review.resolve_memory_review(p, [c.change_id for c in p.changes])
    assert await review.process_memory_review_decision(p) == "applying"
    monkeypatch.setattr(review, "_atomic_write", write)
    (vault / "Topics/External.md").write_text("External accepted change")
    checker = AsyncMock(
        return_value=MemoryFreshnessResult(
            verdict=verdict, reason="External evidence checked"
        )
    )
    monkeypatch.setattr(review, "check_freshness", checker)
    result = await review.process_memory_review_queue()
    assert (vault / "Topics/Plan.md").read_text() == "Plan from September"
    assert (vault / "Topics/External.md").read_text() == "External accepted change"
    assert checker.await_count == 1
    if verdict == "unaffected":
        assert result["applied"] == 1
        assert (vault / "Topics/Second.md").read_text() == "Second fact"
        assert await MemoryAuditEntry.find_all().count() == 2
    else:
        assert result["regenerating"] == 1
        assert not (vault / "Topics/Second.md").exists()
        assert await MemoryAuditEntry.find_all().count() == 1
        old = await MemoryReviewProposal.get(p.id)
        assert old.accepted_change_ids == [p.changes[0].change_id]
        assert old.replacement_proposal_id
        monkeypatch.setattr(
            review,
            "validate_selection",
            AsyncMock(side_effect=review.SelectionChanged("Evidence revised")),
        )
        await review.refresh_memory_selection_states()
        assert (await MemoryReviewProposal.get(old.id)).state == "correction_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_correction_resolves_predecessor_only_after_full_acceptance(
    vault, monkeypatch, partial
):
    old = proposal(
        state="correction_required", active=False, accepted_change_ids=["old-change"]
    )
    await old.insert()
    p = await pending(
        vault,
        request_id="correction",
        correction_of=[old.proposal_id],
        correction_episode_keys=["ep-one"],
    )
    p.changes += review.build_potential_changes(
        {},
        {"Topics/Second.md": "Corrected second fact"},
        source_episode_keys_by_path={"Topics/Second.md": ["ep-one"]},
    )
    await p.save()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    await review.resolve_memory_review(
        p, [p.changes[0].change_id] if partial else [c.change_id for c in p.changes]
    )
    assert await review.process_memory_review_decision(p) == "applied"
    old = await MemoryReviewProposal.get(old.id)
    assert old.state == ("correction_required" if partial else "corrected")
    if not partial:
        assert old.corrected_by_proposal_id == p.proposal_id
