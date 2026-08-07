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
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)


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
    def __init__(self, value):
        self._value = value

    async def count(self):
        return self._value


def _guard_env(monkeypatch, *, day, episode_count):
    async def find_one(*args, **kwargs):
        return day

    monkeypatch.setattr(discovery.TimelineDay, "find_one", find_one)
    monkeypatch.setattr(
        discovery.TimelineEpisode, "find", lambda *a, **k: _FakeCount(episode_count)
    )


def _day(active_run_id="run-one", evidence_count=100):
    return SimpleNamespace(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        active_run_id=active_run_id,
        coverage={"evidence_count": evidence_count},
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
