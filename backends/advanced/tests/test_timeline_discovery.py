import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.timeline import (
    TimelineDay,
    TimelineEpisode,
    TimelineEvidenceRef,
)
from advanced_omi_backend.services.timeline import discovery
from advanced_omi_backend.services.timeline.contracts import (
    TimelineAgentResult,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
    UnassignedInterval,
)


@pytest.mark.asyncio
async def test_invalid_low_and_medium_segmentations_escalate_through_high(
    tmp_path, monkeypatch
):
    started_at = datetime(2026, 8, 6, tzinfo=timezone.utc)
    evidence = TimelineEvidenceItem(
        evidence_id="observation:one",
        kind="observation",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=1),
        role="application_state",
        excerpt="One screen observation",
    )
    manifest = TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=started_at,
        ended_at=started_at + timedelta(days=1),
        evidence_revision="revision",
        windows=[],
        evidence=[evidence],
    )

    class Executor:
        def __init__(self):
            self.efforts = []
            self.feedback = []

        async def analyze(
            self, *args, reasoning_effort=None, validation_feedback=None, **kwargs
        ):
            self.efforts.append(reasoning_effort)
            self.feedback.append(validation_feedback)
            if reasoning_effort != "high":
                return TimelineAgentResult(episodes=[], unassigned_intervals=[])
            return TimelineAgentResult(
                episodes=[],
                unassigned_intervals=[
                    UnassignedInterval(
                        started_at=evidence.started_at,
                        ended_at=evidence.ended_at,
                        reason="Insufficient semantic detail",
                    )
                ],
            )

    executor = Executor()
    monkeypatch.setattr(discovery, "build_executor", lambda: executor)

    result = await discovery._analyze_with_escalation(
        manifest,
        tmp_path,
        [],
        [],
        configured_effort="low",
    )

    assert executor.efforts == ["low", "medium", "high"]
    assert executor.feedback[0] is None
    assert all(
        "no episodes and no unassigned intervals" in value
        for value in executor.feedback[1:]
    )
    assert result.unassigned_intervals


@pytest.mark.asyncio
async def test_processing_records_the_manifest_revision_actually_used(monkeypatch):
    manifest = TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        evidence_revision="processed-revision",
        windows=[],
        evidence=[],
    )

    async def assemble(*args, **kwargs):
        return manifest, {}

    monkeypatch.setattr(discovery, "assemble_day_evidence", assemble)
    monkeypatch.setattr(discovery, "settings_dict", lambda: {})

    class Run(SimpleNamespace):
        async def save(self):
            return self

    run = Run(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        processed_evidence_revision=None,
        coverage_window_ids=[],
        state="preparing",
        completed_at=None,
    )

    await discovery._process_run(run)

    assert run.processed_evidence_revision == "processed-revision"
    assert run.state == "awaiting_evidence"


@pytest.mark.asyncio
async def test_clean_rebuild_omits_unconfirmed_prior_episodes(monkeypatch):
    started_at = datetime(2026, 8, 6, tzinfo=timezone.utc)
    evidence = TimelineEvidenceItem(
        evidence_id="transcript:new",
        kind="transcript",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=5),
        role="uncertain",
        excerpt="new recovered conversation",
    )
    manifest = TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=started_at,
        ended_at=started_at + timedelta(days=1),
        evidence_revision="new-revision",
        windows=[],
        evidence=[evidence],
    )
    prior = [
        SimpleNamespace(confirmed_at=None),
        SimpleNamespace(confirmed_at=started_at),
    ]
    captured = {}

    async def assemble(*args, **kwargs):
        return manifest, {}

    async def analyze(manifest, workspace, existing, pinned, **kwargs):
        captured["existing"] = existing
        captured["pinned"] = pinned
        return TimelineAgentResult(
            episodes=[],
            unassigned_intervals=[
                UnassignedInterval(
                    started_at=evidence.started_at,
                    ended_at=evidence.ended_at,
                    reason="covered by explicit accounting",
                )
            ],
        )

    async def publish(*args, **kwargs):
        return None

    monkeypatch.setattr(discovery, "assemble_day_evidence", assemble)
    monkeypatch.setattr(discovery, "settings_dict", lambda: {})

    async def active(_run):
        return prior

    monkeypatch.setattr(discovery, "_active_episodes", active)
    monkeypatch.setattr(discovery, "_analyze_with_escalation", analyze)
    monkeypatch.setattr(discovery, "_publish", publish)
    monkeypatch.setattr(discovery, "write_workspace", lambda *args, **kwargs: None)

    class Run(SimpleNamespace):
        async def save(self):
            return self

    run = Run(
        run_id="run-clean-rebuild",
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        executor="pi",
        prompt_version="v1",
        processed_evidence_revision=None,
        coverage_window_ids=[],
        state="preparing",
        usage={},
        completed_at=None,
    )

    await discovery._process_run(run, retain_unconfirmed_existing=False)

    assert captured["existing"] == []
    assert captured["pinned"] == [prior[1]]


@pytest.mark.asyncio
async def test_claimed_run_failure_records_visible_system_error(monkeypatch):
    events = []

    async def process(_run):
        raise ValueError("context output was truncated")

    class Run(SimpleNamespace):
        async def save(self):
            return self

    run = Run(
        run_id="run-failed",
        user_id="user-42",
        local_date=date(2026, 7, 23),
        timezone="UTC",
        state="running",
        error=None,
        completed_at=None,
    )
    monkeypatch.setattr(discovery, "_process_run", process)
    monkeypatch.setattr(discovery, "settings_dict", lambda: {})
    monkeypatch.setattr(
        discovery, "record_event_sync", lambda **event: events.append(event)
    )

    result = await discovery._run_claimed(run)

    assert result == {"processed": 0, "failed": 1, "deferred": 0}
    assert run.state == "failed"
    assert run.error == "ValueError: context output was truncated"
    assert events == [
        {
            "severity": "error",
            "category": "pipeline",
            "source": "timeline.analysis",
            "title": "Timeline analysis failed for 2026-07-23",
            "detail": "ValueError: context output was truncated",
            "user_id": "user-42",
            "metadata": {
                "run_id": "run-failed",
                "local_date": "2026-07-23",
                "timezone": "UTC",
            },
            "incident_key": "timeline.analysis:user-42:2026-07-23",
        }
    ]


@pytest.mark.asyncio
async def test_claimed_run_success_resolves_the_day_incident(monkeypatch):
    events = []

    async def process(_run):
        return None

    run = SimpleNamespace(
        run_id="run-repaired",
        user_id="user-42",
        local_date=date(2026, 7, 23),
        timezone="UTC",
    )
    monkeypatch.setattr(discovery, "_process_run", process)
    monkeypatch.setattr(discovery, "settings_dict", lambda: {})
    monkeypatch.setattr(
        discovery, "record_event_sync", lambda **event: events.append(event)
    )

    result = await discovery._run_claimed(run)

    assert result == {"processed": 1, "failed": 0, "deferred": 0}
    assert events == [
        {
            "severity": "info",
            "category": "pipeline",
            "source": "timeline.analysis",
            "title": "Timeline analysis recovered for 2026-07-23",
            "user_id": "user-42",
            "metadata": {
                "run_id": "run-repaired",
                "local_date": "2026-07-23",
                "timezone": "UTC",
            },
            "incident_key": "timeline.analysis:user-42:2026-07-23",
            "resolves_incident": True,
        }
    ]


def test_evidence_refs_keep_the_conversation_id_they_were_assembled_with():
    """Without this, an episode cannot link back to the recording it cites."""

    item = TimelineEvidenceItem(
        evidence_id="audio_span:one",
        kind="audio_span",
        started_at=datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc),
        role="uncertain",
        metadata={"conversation_id": "conv-42", "direction": "input"},
    )

    ref = discovery._evidence_ref(item)

    assert ref.metadata["conversation_id"] == "conv-42"


@pytest.fixture
async def episode_documents(mongo_service):
    """Beanie must know the collection before a ``TimelineEpisode`` can be built.

    Carry-forward is pure logic, but it produces real documents and we want their
    validators to run — so this initializes the model rather than bypassing it with
    ``model_construct``.
    """

    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_timeline_discovery_db"]
    await init_beanie(database=database, document_models=[TimelineEpisode, TimelineDay])
    yield
    await client.drop_database("test_timeline_discovery_db")
    client.close()


def _confirmed_episode(**overrides) -> TimelineEpisode:
    start = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    payload = {
        "episode_id": "old-row",
        "episode_key": "durable-key",
        "run_id": "run-one",
        "user_id": "user",
        "local_date": date(2026, 8, 6),
        "timezone": "UTC",
        "started_at": start,
        "ended_at": start + timedelta(minutes=30),
        "kind": "gaming_session",
        "title": "Played with Daksh",
        "summary": "A human wrote this.",
        "status": "confirmed",
        "confidence": 0.8,
        "activity_mode": "foreground",
        "confirmed_fields": ["title"],
        "evidence_refs": [
            TimelineEvidenceRef(
                evidence_id="audio_span:one",
                kind="audio_span",
                started_at=start,
                ended_at=start + timedelta(minutes=30),
                role="uncertain",
                metadata={"conversation_id": "conv-42"},
            )
        ],
    }
    payload.update(overrides)
    return TimelineEpisode(**payload)


def _manifest_with(evidence: list[TimelineEvidenceItem]) -> TimelineEvidenceManifest:
    return TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        evidence_revision="revision",
        windows=[],
        evidence=evidence,
    )


@pytest.mark.asyncio
async def test_forced_reanalysis_can_replace_a_non_positive_prior_episode(
    episode_documents,
):
    start = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    valid = _confirmed_episode(status="provisional")
    await valid.insert()
    collection = TimelineEpisode.get_pymongo_collection()
    collapsed = await collection.find_one({"episode_id": "old-row"})
    collapsed.pop("_id")
    collapsed.update(
        {
            "episode_id": "collapsed-row",
            "started_at": start,
            "ended_at": start,
        }
    )
    await collection.insert_one(collapsed)
    await TimelineDay(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        active_run_id="run-one",
    ).insert()

    episodes = await discovery._active_episodes(
        SimpleNamespace(
            user_id="user",
            local_date=date(2026, 8, 6),
            timezone="UTC",
        )
    )

    assert [episode.episode_id for episode in episodes] == ["old-row"]


def test_confirmed_episodes_are_carried_into_the_next_generation(episode_documents):
    episode = _confirmed_episode()
    run = SimpleNamespace(run_id="run-two")

    carried = discovery._carry_forward(run, [episode], _manifest_with([]))

    assert len(carried) == 1
    assert carried[0].episode_key == "durable-key"
    assert carried[0].run_id == "run-two"
    assert carried[0].episode_id != "old-row"
    assert carried[0].title == "Played with Daksh"
    assert carried[0].status == "confirmed"
    assert carried[0].confirmed_fields == ["title"]


def test_carried_episodes_refresh_evidence_that_was_reassembled(episode_documents):
    episode = _confirmed_episode()
    start = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    refreshed = TimelineEvidenceItem(
        evidence_id="audio_span:one",
        kind="audio_span",
        started_at=start,
        ended_at=start + timedelta(minutes=30),
        role="user_statement",
        excerpt="a newly transcribed excerpt",
        metadata={"conversation_id": "conv-42"},
    )
    run = SimpleNamespace(run_id="run-two")

    carried = discovery._carry_forward(run, [episode], _manifest_with([refreshed]))

    assert carried[0].evidence_refs[0].excerpt == "a newly transcribed excerpt"
    assert carried[0].evidence_refs[0].role == "user_statement"


def test_carried_episodes_keep_citations_that_aged_out_of_the_manifest(
    episode_documents,
):
    episode = _confirmed_episode()
    run = SimpleNamespace(run_id="run-two")

    carried = discovery._carry_forward(run, [episode], _manifest_with([]))

    assert [ref.evidence_id for ref in carried[0].evidence_refs] == ["audio_span:one"]
    assert carried[0].evidence_refs[0].metadata["conversation_id"] == "conv-42"


def test_generated_episodes_are_provisional_so_reanalysis_can_replace_them(
    episode_documents,
):
    """A "confirmed" default would pin every episode and make reanalysis a no-op."""

    start = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    episode = TimelineEpisode(
        run_id="run-one",
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=start,
        ended_at=start + timedelta(minutes=5),
        kind="work",
        title="Drafted by the agent",
        summary="",
        confidence=0.5,
        activity_mode="foreground",
    )

    assert episode.status == "provisional"
    assert episode.confirmed_at is None


class _FakeCount:
    def __init__(self, value, rows=()):
        self._value = value
        self._rows = list(rows)

    async def count(self):
        return self._value

    async def to_list(self):
        return self._rows


def _guard_env(monkeypatch, *, day, episode_count, episode_rows=()):
    async def find_one(*args, **kwargs):
        return day

    async def valid_episodes(*args, **kwargs):
        return list(episode_rows)

    monkeypatch.setattr(discovery.TimelineDay, "find_one", find_one)
    monkeypatch.setattr(discovery, "_valid_episodes_for_run", valid_episodes)
    monkeypatch.setattr(
        discovery.TimelineEpisode,
        "find",
        lambda *a, **k: _FakeCount(episode_count, episode_rows),
    )


def _day(active_run_id="run-one", evidence_count=100):
    return SimpleNamespace(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        active_run_id=active_run_id,
        coverage={"evidence_count": evidence_count, "unassigned_intervals": []},
    )


def _run():
    return SimpleNamespace(
        run_id="run-two", user_id="user", local_date=date(2026, 8, 6), timezone="UTC"
    )


def _manifest_of(n_evidence):
    start = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    return _manifest_with(
        [
            TimelineEvidenceItem(
                evidence_id=f"observation:{index}",
                kind="observation",
                started_at=start + timedelta(minutes=index),
                ended_at=start + timedelta(minutes=index + 1),
                role="application_state",
            )
            for index in range(n_evidence)
        ]
    )


async def test_empty_generation_does_not_replace_a_day_that_had_episodes(
    monkeypatch, episode_documents
):
    """One flaky segmentation pass must not blank a day the user could just see."""

    _guard_env(monkeypatch, day=_day(), episode_count=8)

    with pytest.raises(discovery.TimelineEmptyGeneration):
        await discovery._guard_empty_generation(_run(), _manifest_of(100), 0)


async def test_a_generation_with_episodes_always_publishes(
    monkeypatch, episode_documents
):
    _guard_env(monkeypatch, day=_day(), episode_count=8)

    await discovery._guard_empty_generation(_run(), _manifest_of(100), 5)


async def test_material_unexplained_coverage_regression_keeps_previous_generation(
    monkeypatch,
):
    """A partial non-empty response must not replace a fully covered active day."""

    _guard_env(monkeypatch, day=_day(), episode_count=8)
    manifest = _manifest_of(100)
    evidence_start = manifest.evidence[0].started_at
    result = SimpleNamespace(
        unassigned_intervals=[
            UnassignedInterval(
                started_at=evidence_start + timedelta(minutes=70),
                ended_at=evidence_start + timedelta(minutes=100),
                reason="model stopped before the end",
                cause="unexplained",
            )
        ]
    )

    with pytest.raises(discovery.TimelineCoverageRegression):
        await discovery._guard_coverage_regression(_run(), manifest, result)


async def test_small_unexplained_difference_does_not_flap_generation(
    monkeypatch,
):
    _guard_env(monkeypatch, day=_day(), episode_count=8)
    manifest = _manifest_of(100)
    evidence_start = manifest.evidence[0].started_at
    result = SimpleNamespace(
        unassigned_intervals=[
            UnassignedInterval(
                started_at=evidence_start + timedelta(minutes=98),
                ended_at=evidence_start + timedelta(minutes=100),
                reason="brief uncertain tail",
                cause="unexplained",
            )
        ]
    )

    await discovery._guard_coverage_regression(_run(), manifest, result)


async def test_uncaptured_interior_removed_from_bad_episode_is_not_coverage_regression(
    monkeypatch,
):
    """Replacing a bridge over empty time must not look like lost captured coverage."""

    _guard_env(monkeypatch, day=_day(evidence_count=2), episode_count=1)
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    manifest = _manifest_with(
        [
            TimelineEvidenceItem(
                evidence_id="observation:early",
                kind="observation",
                started_at=start,
                ended_at=start + timedelta(minutes=1),
                role="application_state",
            ),
            TimelineEvidenceItem(
                evidence_id="observation:late",
                kind="observation",
                started_at=start + timedelta(hours=11),
                ended_at=start + timedelta(hours=11, minutes=1),
                role="application_state",
            ),
        ]
    )
    result = SimpleNamespace(
        unassigned_intervals=[
            UnassignedInterval(
                started_at=start + timedelta(minutes=1),
                ended_at=start + timedelta(hours=11),
                reason="No captured evidence between separate activities",
                cause="unexplained",
            )
        ]
    )

    await discovery._guard_coverage_regression(_run(), manifest, result)


async def test_invalid_prior_bridge_does_not_block_a_lower_coverage_repair(monkeypatch):
    start = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    manifest = _manifest_of(100)
    manifest.evidence.append(
        TimelineEvidenceItem(
            evidence_id="observation:far-later",
            kind="observation",
            started_at=start + timedelta(hours=11),
            ended_at=start + timedelta(hours=11, minutes=1),
            role="application_state",
        )
    )
    prior_bridge = SimpleNamespace(
        episode_id="bad-prior",
        # Motor returns UTC datetimes without tzinfo unless tz-aware decoding is on.
        started_at=start.replace(tzinfo=None),
        ended_at=(start + timedelta(hours=11, minutes=1)).replace(tzinfo=None),
    )
    _guard_env(
        monkeypatch,
        day=_day(evidence_count=len(manifest.evidence)),
        episode_count=1,
        episode_rows=[prior_bridge],
    )
    result = SimpleNamespace(
        unassigned_intervals=[
            UnassignedInterval(
                started_at=start + timedelta(minutes=70),
                ended_at=start + timedelta(minutes=100),
                reason="New draft left captured evidence unexplained",
                cause="unexplained",
            )
        ]
    )

    await discovery._guard_coverage_regression(_run(), manifest, result)


async def test_empty_generation_is_allowed_when_the_day_had_no_episodes(
    monkeypatch, episode_documents
):
    _guard_env(monkeypatch, day=_day(), episode_count=0)

    await discovery._guard_empty_generation(_run(), _manifest_of(100), 0)


async def test_empty_generation_is_allowed_when_evidence_shrank(
    monkeypatch, episode_documents
):
    """Captures were deleted, so having nothing to say is the honest answer."""

    _guard_env(monkeypatch, day=_day(evidence_count=100), episode_count=8)

    await discovery._guard_empty_generation(_run(), _manifest_of(20), 0)


async def test_empty_generation_is_allowed_on_a_day_never_analyzed(
    monkeypatch, episode_documents
):
    _guard_env(monkeypatch, day=None, episode_count=0)

    await discovery._guard_empty_generation(_run(), _manifest_of(100), 0)
